"""Persistent, read-only inventory of the user's explicitly connected music folders.

Playlist membership and physical storage are separate. Scanning never copies,
tags, renames or deletes music. Unique matches and explicit choices persist IDs.
"""
from __future__ import annotations

import math
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import mutagen

from .atomic_io import is_partial_path

AUDIO_EXTENSIONS = {'.flac', '.mp3', '.wav', '.aiff', '.aif', '.m4a', '.aac', '.opus', '.ogg'}
_LOCK_GUARD = threading.Lock()
_SCAN_LOCKS: dict[str, threading.RLock] = {}


class MusicRootRequired(ValueError):
    code = 'music_root_required'

    def __init__(self):
        super().__init__('Сначала выберите общую папку с музыкой.')


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _normal(value: object) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[\W_]+', ' ', value, flags=re.UNICODE)).strip()


def _identity(track: dict) -> tuple[str, str]:
    provider = str(track.get('provider') or 'deezer')
    if provider == 'soundcloud':
        provider = 'sc'
    if provider not in {'deezer', 'sc'}:
        raise ValueError('Неизвестный источник трека.')
    raw = str(track.get('id') or '')
    if raw.startswith(provider + ':'):
        raw = raw[len(provider) + 1:]
    if not raw or len(raw) > 512:
        raise ValueError('Не указан идентификатор трека.')
    return provider, raw


def configured_music_roots(config: dict) -> list[Path]:
    raw = config.get('music_root')
    if not isinstance(raw, str) or not raw.strip() or not Path(raw).is_absolute():
        raise MusicRootRequired()
    # A previously configured removable disk can be offline: retain its identity
    # so it does not look like an empty catalog and trigger duplicate downloads.
    roots = [Path(raw)]
    bindings = config.get('bindings') or {}
    if isinstance(bindings, dict):
        for path in bindings.values():
            if isinstance(path, str) and Path(path).is_absolute():
                roots.append(Path(path))
    unique = {_path_key(path): path for path in roots}
    return list(unique.values())


def _metadata(path: Path) -> dict:
    result = dict(title='', artist='', duration=0.0, source='filename', valid=False)
    try:
        audio = mutagen.File(path, easy=False)
        duration = float(getattr(getattr(audio, 'info', None), 'length', 0) or 0)
        if audio is None or not math.isfinite(duration) or duration <= 0:
            return result
        result.update(valid=True, duration=duration)
        tags = {str(key).casefold(): value for key, value in (audio.tags or {}).items()}

        def values(*keys):
            for key in keys:
                value = tags.get(key)
                if value is not None:
                    value = getattr(value, 'text', value)
                    if isinstance(value, (tuple, list)):
                        value = ', '.join(str(item) for item in value if str(item))
                    if str(value).strip():
                        return str(value).strip()
            return ''

        result['title'] = values('title', 'tit2', '\xa9nam')
        result['artist'] = values('artist', 'tpe1', '\xa9art')
        if result['title'] and result['artist']:
            result['source'] = 'metadata'
        stem = re.sub(r'^\s*\d{1,4}\s*[-._)]\s*', '', path.stem)
        if ' - ' in stem:
            artist, title = stem.split(' - ', 1)
            result['title'] = result['title'] or title.strip()
            result['artist'] = result['artist'] or artist.strip()
    except (OSError, ValueError, mutagen.MutagenError):
        pass
    return result


class MusicCatalog:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK_GUARD:
            self.scan_lock = _SCAN_LOCKS.setdefault(_path_key(self.path), threading.RLock())
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS roots (
                    key TEXT PRIMARY KEY, path TEXT NOT NULL,
                    state TEXT NOT NULL, scanned_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    key TEXT PRIMARY KEY, path TEXT NOT NULL, root_key TEXT NOT NULL,
                    title TEXT NOT NULL, artist TEXT NOT NULL,
                    norm_title TEXT NOT NULL, norm_artist TEXT NOT NULL,
                    duration REAL NOT NULL, format TEXT NOT NULL,
                    metadata_source TEXT NOT NULL, state TEXT NOT NULL,
                    fs_id TEXT NOT NULL, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, seen TEXT NOT NULL,
                    FOREIGN KEY(root_key) REFERENCES roots(key)
                );
                CREATE INDEX IF NOT EXISTS file_metadata ON files(norm_title,norm_artist);
                CREATE INDEX IF NOT EXISTS file_identity ON files(fs_id,size);
                CREATE TABLE IF NOT EXISTS identities (
                    provider TEXT NOT NULL, track_id TEXT NOT NULL,
                    file_key TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(provider,track_id,file_key),
                    FOREIGN KEY(file_key) REFERENCES files(key)
                );
                CREATE TABLE IF NOT EXISTS sidecar_observations (
                    entry_key TEXT PRIMARY KEY, signature TEXT NOT NULL
                );
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def scan(self, roots: list[Path], progress=None) -> dict:
        """Atomically publish a scan; unavailable/partial roots retain old rows."""
        candidates = sorted({Path(p).resolve() for p in roots}, key=lambda p: len(p.parts))
        selected: list[Path] = []
        for path in candidates:
            if not any(path == parent or parent in path.parents for parent in selected):
                selected.append(path)
        stamp = str(time.time_ns())
        counters = dict(files=0, invalid=0, offline_roots=[], partial_roots=[], scanned_at=time.time())
        with self.scan_lock, self._connection() as db:
            db.execute("UPDATE roots SET state='detached'")
            for root in selected:
                root_key = _path_key(root)
                db.execute('''INSERT INTO roots(key,path,state,scanned_at) VALUES(?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET path=excluded.path,state=excluded.state,scanned_at=excluded.scanned_at''',
                    (root_key, str(root), 'online', counters['scanned_at']))
                if not root.is_dir():
                    db.execute("UPDATE roots SET state='offline' WHERE key=?", (root_key,))
                    counters['offline_roots'].append(str(root))
                    continue
                complete = True
                stack = [root]
                sidecar_dirs = []
                while stack:
                    directory = stack.pop()
                    try:
                        with os.scandir(directory) as entries:
                            children = sorted(entries, key=lambda item: item.name.casefold())
                    except OSError:
                        complete = False
                        continue
                    if any(e.name in {'.deckpipe.json', '.deckpipe.json.bak'} for e in children):
                        sidecar_dirs.append(directory)
                    for child in children:
                        try:
                            stat = child.stat(follow_symlinks=False)
                            if child.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                                continue
                            path = Path(child.path)
                            if child.is_dir(follow_symlinks=False):
                                stack.append(path)
                                continue
                            if not child.is_file(follow_symlinks=False) or path.suffix.lower() not in AUDIO_EXTENSIONS or is_partial_path(path):
                                continue
                            resolved = path.resolve()
                            if root not in resolved.parents:
                                continue
                            self._index_file(db, resolved, root_key, stat, stamp)
                            counters['files'] += 1
                            if progress and counters['files'] % 100 == 0:
                                progress(dict(counters))
                        except OSError:
                            complete = False
                    # No following of symlinks/junctions: the approved roots are
                    # the complete boundary, not a starting point for disk search.
                for directory in sidecar_dirs:
                    if not self._index_sidecar(db, directory):
                        complete = False
                state = 'online' if complete else 'partial'
                db.execute('UPDATE roots SET state=? WHERE key=?', (state, root_key))
                if complete:
                    db.execute("UPDATE files SET state='missing' WHERE root_key=? AND seen<>?", (root_key, stamp))
                else:
                    counters['partial_roots'].append(str(root))
            counters['invalid'] = db.execute("SELECT count(*) FROM files f JOIN roots r ON r.key=f.root_key WHERE r.state<>'detached' AND f.state='invalid'").fetchone()[0]
        return counters

    def _index_file(self, db, path, root_key, stat, stamp):
        key = _path_key(path)
        # Windows DirEntry.stat() may omit the file index; Path.stat() retrieves
        # it. A zero index cannot identify a move between playlist folders.
        if not stat.st_ino:
            stat = path.stat()
        fs_id = f'{stat.st_dev}:{stat.st_ino}' if stat.st_ino else ''
        old = db.execute('SELECT * FROM files WHERE key=?', (key,)).fetchone()
        if old and old['mtime_ns'] == stat.st_mtime_ns and old['size'] == stat.st_size and old['fs_id'] == fs_id:
            db.execute("UPDATE files SET path=?,root_key=?,state=CASE WHEN state='invalid' THEN state ELSE 'available' END,seen=? WHERE key=?",
                       (str(path), root_key, stamp, key))
            return
        metadata = _metadata(path)
        if old:
            # A path is not an immutable audio identity. Replacement or edits
            # invalidate the old association; unchanged sidecars cannot restore it.
            db.execute('DELETE FROM identities WHERE file_key=?', (key,))
        db.execute('''INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET path=excluded.path,root_key=excluded.root_key,
            title=excluded.title,artist=excluded.artist,norm_title=excluded.norm_title,norm_artist=excluded.norm_artist,
            duration=excluded.duration,format=excluded.format,metadata_source=excluded.metadata_source,
            state=excluded.state,fs_id=excluded.fs_id,size=excluded.size,mtime_ns=excluded.mtime_ns,seen=excluded.seen''',
            (key, str(path), root_key, metadata['title'], metadata['artist'],
             _normal(metadata['title']), _normal(metadata['artist']), metadata['duration'], path.suffix[1:].lower(),
             metadata['source'], 'available' if metadata['valid'] else 'invalid', fs_id, stat.st_size, stat.st_mtime_ns, stamp))
        if fs_id and not old:
            # A rename on the same volume keeps its file identifier. Transfer
            # confirmed identities only when the old path has actually gone.
            previous = db.execute('SELECT key,path FROM files WHERE fs_id=? AND size=? AND mtime_ns=? AND key<>?', (fs_id, stat.st_size, stat.st_mtime_ns, key)).fetchall()
            for row in previous:
                if not Path(row['path']).exists():
                    db.execute('''INSERT OR IGNORE INTO identities SELECT provider,track_id,?,source
                                  FROM identities WHERE file_key=?''', (key, row['key']))

    def _index_sidecar(self, db, directory: Path) -> bool:
        from .library import load_sidecar
        try:
            sidecar = load_sidecar(directory)
            for raw_id, entry in sidecar.get('tracks', {}).items():
                entry_key = json.dumps([_path_key(directory), raw_id])
                signature = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
                previous = db.execute('SELECT signature FROM sidecar_observations WHERE entry_key=?', (entry_key,)).fetchone()
                if previous and previous['signature'] == signature:
                    continue
                if entry.get('status') != 'ok':
                    continue
                filename = entry.get('file')
                if not isinstance(filename, str) or Path(filename).name != filename or is_partial_path(filename):
                    continue
                path = directory / filename
                key = _path_key(path)
                row = db.execute("SELECT key FROM files WHERE key=? AND state='available'", (key,)).fetchone()
                if row is None:
                    continue
                provider = entry.get('provider') or ('sc' if str(raw_id).startswith('sc:') else 'deezer')
                provider, tid = _identity({'provider': provider, 'id': raw_id})
                db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?,?)', (provider, tid, key, 'sidecar'))
                db.execute('INSERT INTO sidecar_observations VALUES(?,?) ON CONFLICT(entry_key) DO UPDATE SET signature=excluded.signature', (entry_key, signature))
            return True
        except (ValueError, RuntimeError, OSError):
            return False

    @staticmethod
    def _available(row) -> bool:
        if row['state'] != 'available' or row['root_state'] == 'detached':
            return False
        path = Path(row['path'])
        try:
            stat = path.stat()
            fs_id = f'{stat.st_dev}:{stat.st_ino}' if stat.st_ino else ''
            return path.is_file() and not path.is_symlink() and fs_id == row['fs_id'] and stat.st_size == row['size'] and stat.st_mtime_ns == row['mtime_ns']
        except OSError:
            return False

    @staticmethod
    def _location(row) -> dict:
        path = Path(row['path'])
        return dict(path=str(path), folder=str(path.parent), file=path.name, format=row['format'], duration=row['duration'])

    def match(self, track: dict) -> dict:
        provider, tid = _identity(track)
        with self._connection() as db:
            rows = db.execute('''SELECT f.*,r.state AS root_state,i.source FROM identities i
                JOIN files f ON f.key=i.file_key JOIN roots r ON r.key=f.root_key
                WHERE i.provider=? AND i.track_id=? AND r.state<>'detached' ORDER BY f.path''', (provider, tid)).fetchall()
            live = [row for row in rows if self._available(row)]
            if live:
                conflicting = any(db.execute('''SELECT 1 FROM identities
                    WHERE file_key=? AND provider=? AND track_id<>? LIMIT 1''',
                    (row['key'], provider, tid)).fetchone() for row in live if row['source'] != 'confirmed')
                if conflicting:
                    return dict(status='ambiguous', match_source='identity_conflict', locations=[self._location(row) for row in live])
                return dict(status='ok', match_source=live[0]['source'], locations=[self._location(row) for row in live])
            title, artist = _normal(track.get('title')), _normal(track.get('artist'))
            matches = []
            if title and artist:
                candidates = db.execute('''SELECT f.*,r.state AS root_state FROM files f JOIN roots r ON r.key=f.root_key
                    WHERE norm_title=? AND norm_artist=? AND r.state<>'detached' ORDER BY f.path''', (title, artist)).fetchall()
                for row in candidates:
                    if not self._available(row):
                        continue
                    owner = db.execute('SELECT 1 FROM identities WHERE file_key=? AND provider=? AND track_id<>? LIMIT 1', (row['key'], provider, tid)).fetchone()
                    if owner:
                        continue
                    try:
                        expected = float(track.get('duration') or 0)
                    except (ValueError, TypeError):
                        expected = 0
                    if expected > 0 and abs(row['duration'] - expected) > 2.0:
                        continue
                    matches.append(row)
            if matches:
                if len(matches) == 1:
                    db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?,?)',
                               (provider, tid, matches[0]['key'], matches[0]['metadata_source']))
                return dict(status='ok' if len(matches) == 1 else 'ambiguous',
                            match_source=matches[0]['metadata_source'] if len(matches) == 1 else 'ambiguous',
                            locations=[self._location(row) for row in matches])
            unavailable = db.execute("SELECT path,state FROM roots WHERE state IN ('offline','partial')").fetchall()
            # Recheck a removed drive even before an explicit rescan.
            roots = db.execute("SELECT path FROM roots WHERE state='online'").fetchall()
            offline = bool(unavailable) or any(not Path(row['path']).is_dir() for row in roots)
            return dict(status='offline' if offline else 'missing', match_source='', locations=[])

    def confirm(self, track: dict, path: str) -> dict:
        provider, tid = _identity(track)
        key = _path_key(Path(path))
        with self.scan_lock, self._connection() as db:
            row = db.execute('SELECT f.*,r.state AS root_state FROM files f JOIN roots r ON r.key=f.root_key WHERE f.key=?', (key,)).fetchone()
            if row is None or not self._available(row):
                raise ValueError('Выберите доступный файл из подключённой музыкальной библиотеки.')
            db.execute('DELETE FROM identities WHERE provider=? AND track_id=?', (provider, tid))
            db.execute('INSERT INTO identities VALUES(?,?,?,?)', (provider, tid, key, 'confirmed'))
        return self.match(track)

    def status(self) -> dict:
        with self._connection() as db:
            roots = [dict(row) for row in db.execute("SELECT path,state,scanned_at FROM roots WHERE state<>'detached' ORDER BY path")]
            count = db.execute("SELECT count(*) FROM files f JOIN roots r ON r.key=f.root_key WHERE f.state='available' AND r.state<>'detached'").fetchone()[0]
        return {'roots': roots, 'files': count, 'configured': bool(roots)}


def catalog_for_config(config=None) -> MusicCatalog:
    from .deezer_client import ROOT, load_config
    configured_music_roots(load_config() if config is None else config)
    return MusicCatalog(ROOT / 'library-catalog.sqlite3')
