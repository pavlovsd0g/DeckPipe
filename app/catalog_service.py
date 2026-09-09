"""Common-root setup, cross-folder playlist views, and missing-only downloads."""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from . import library
from .deezer_client import load_config
from .library_catalog import MusicRootRequired, catalog_for_config, configured_music_roots

_STATE_LOCK = threading.RLock()
_scan_state = {'state': 'idle', 'files': 0, 'error': ''}
_requested_config = None
_generation = 0
_scan_thread = None


class LibraryUnavailable(RuntimeError):
    code = 'music_root_unavailable'

    def __init__(self):
        super().__init__('Музыкальная папка недоступна или прочитана не полностью. Подключите носитель и повторите сканирование.')


def validate_root(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise MusicRootRequired()
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError('Выберите существующую общую папку с музыкой.')
    try:
        next(candidate.iterdir(), None)
    except OSError:
        raise LibraryUnavailable() from None
    return candidate.resolve()


def scan_status() -> dict:
    config = load_config()
    try:
        catalog = catalog_for_config(config)
    except MusicRootRequired:
        return {'configured': False, 'state': 'root_required', 'files': 0, 'roots': [], 'error': ''}
    result = catalog.status()
    with _STATE_LOCK:
        result.update(copy.deepcopy(_scan_state))
    # Music root has been explicitly saved, even before its first scan.
    result['configured'] = True
    return result


def begin_scan() -> dict:
    global _requested_config, _scan_thread, _generation, _scan_state
    config = load_config()
    configured_music_roots(config)
    with _STATE_LOCK:
        _generation += 1
        _requested_config = copy.deepcopy(config)
        _scan_state = {'state': 'scanning', 'files': 0, 'error': ''}
        if _scan_thread is None or not _scan_thread.is_alive():
            _scan_thread = threading.Thread(target=_scan_loop, name='DeckPipe-library-scan', daemon=True)
            _scan_thread.start()
    return scan_status()


def _scan_loop():
    global _scan_state, _scan_thread
    while True:
        with _STATE_LOCK:
            generation = _generation
            config = copy.deepcopy(_requested_config)

        def progress(summary):
            with _STATE_LOCK:
                if generation == _generation:
                    _scan_state.update(files=summary['files'])

        try:
            summary = catalog_for_config(config).scan(configured_music_roots(config), progress=progress)
            state = 'offline' if summary['offline_roots'] or summary['partial_roots'] else 'ready'
            result = {**summary, 'state': state, 'error': str(LibraryUnavailable()) if state == 'offline' else ''}
        except Exception:
            result = {'state': 'error', 'files': 0, 'error': 'Не удалось прочитать каталог музыки. Повторите сканирование.'}
        with _STATE_LOCK:
            if generation != _generation:
                continue
            _scan_state = result
            _scan_thread = None
            return


def validate_destination(pl_dir: Path, *, require_online: bool = False, config=None) -> Path:
    config = load_config() if config is None else config
    roots = configured_music_roots(config)
    target = Path(pl_dir).resolve()
    if not any(target == root.resolve() or root.resolve() in target.parents for root in roots):
        raise ValueError('Папка плейлиста должна быть внутри библиотеки или явно привязана.')
    if require_online and any(not root.is_dir() for root in roots):
        raise LibraryUnavailable()
    return target


def playlist_tracks(pl_dir: Path, requested: list[dict], *, refresh: bool = True) -> list[dict]:
    config = load_config()
    roots = configured_music_roots(config)
    catalog = catalog_for_config(config)
    target = validate_destination(pl_dir, config=config)
    # Local status is read without adopting filenames into trusted identities.
    # The catalog validates duration and ambiguity across every connected folder.
    local = library.scan_playlist(pl_dir, requested, adopt_unmatched=False)
    if refresh or not catalog.status()['configured']:
        catalog.scan(roots)
    result = []
    for item in local:
        match = catalog.match(item)
        locations = match['locations']
        row = {**item, 'locations': locations, 'match_source': match['match_source'],
               'file_path': '', 'folder': '', 'location_scope': '', 'can_renumber': False}
        if item['status'] == 'error':
            # An existing conversion/validation failure is an operation to retry,
            # not a successful library match just because its source is readable.
            result.append(row)
            continue
        row['status'] = match['status']
        if match['status'] == 'ok':
            chosen = next((loc for loc in locations if Path(loc['folder']).resolve() == target), locations[0])
            is_local = Path(chosen['folder']).resolve() == target
            row.update(file=chosen['file'], file_path=chosen['path'], folder=chosen['folder'],
                       format=chosen['format'], error='', location_scope='playlist' if is_local else 'library',
                       can_renumber=is_local)
        elif match['status'] == 'ambiguous':
            row['error'] = 'Найдено несколько совпадений или конфликт идентификаторов. Выберите файл.'
        elif match['status'] == 'offline':
            row['error'] = str(LibraryUnavailable())
        elif item['status'] == 'ok':
            row.update(status='error', error='Файл изменён или не читается как аудио. Проверьте его и повторите сканирование.')
        result.append(row)
    return result


def prepare_download(pl_dir: Path, requested: list[dict]) -> dict:
    config = load_config()
    roots = configured_music_roots(config)
    # This check runs before local scan could create a disconnected root path.
    if any(not root.is_dir() for root in roots):
        raise LibraryUnavailable()
    rows = playlist_tracks(pl_dir, requested)
    if any(row['status'] == 'offline' for row in rows):
        raise LibraryUnavailable()
    selected = {(str(t.get('provider') or 'deezer'), str(t['id'])): t for t in requested}
    missing = [selected[(str(row.get('provider') or 'deezer'), str(row['id']))]
               for row in rows if row['status'] == 'missing']
    return {'tracks': missing,
            'already_present': sum(row['status'] == 'ok' for row in rows),
            'needs_attention': sum(row['status'] not in {'ok', 'missing'} for row in rows)}


def existing_download(track: dict) -> dict | None:
    """Worker recheck; unconfigured legacy unit/internal flows keep their contract.

    Public download endpoints enforce the mandatory root before enqueueing.
    A queued item rechecks disk availability immediately before provider access.
    """
    config = load_config()
    if not config.get('music_root'):
        return None
    catalog = catalog_for_config(config)
    roots = configured_music_roots(config)
    # Refresh the inventory at the worker boundary too: another queued job or
    # the user may have created/moved the file since the request's preflight.
    # Unchanged files retain cached metadata, so only new/changed audio is read.
    catalog.scan(roots)
    result = catalog.match(track)
    if result['status'] in {'offline', 'ambiguous'}:
        raise LibraryUnavailable()
    return result if result['status'] == 'ok' else None


def confirm_location(track: dict, path: str) -> dict:
    catalog = catalog_for_config()
    return catalog.confirm(track, path)


def reconcile_reused_download(pl_dir: Path, track: dict) -> None:
    """Clear a satisfied download retry without inserting a cross-folder path."""
    from .atomic_io import file_lock
    key = library.track_key(track['id'], track.get('provider'))
    if not library.sidecar_path(pl_dir).exists():
        return
    with file_lock(library.sidecar_path(pl_dir)):
        sidecar = library.load_sidecar(pl_dir)
        entry = sidecar.get('tracks', {}).get(key, {})
        if entry.get('status') in {'verify_failed_download', 'verify_failed_metadata'} and not entry.get('source_file'):
            del sidecar['tracks'][key]
            library.save_sidecar(pl_dir, sidecar)
