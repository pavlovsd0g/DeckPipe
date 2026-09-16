"""Authoritative source-to-catalog boundary and reversible Rekordbox media flows."""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import catalog_service, library, rekordbox as rb
from .atomic_io import file_lock
from .activity_log import record
from .library_catalog import MusicRootRequired
from .rekordbox_media import MediaError, MediaStore, path_key


class SourceError(RuntimeError):
    def __init__(self, code, unresolved=None):
        self.code = code
        self.unresolved = unresolved or [{'code': code}]
        super().__init__(code)


def canonical_tracks(tracks, default_provider='deezer'):
    if not isinstance(tracks, list) or getattr(tracks, 'errors', None):
        raise SourceError('source_incomplete')
    result, identities = [], set()
    for item in tracks:
        if not isinstance(item, dict):
            raise SourceError('source_incomplete')
        provider = item.get('provider') or default_provider
        provider = 'sc' if provider == 'soundcloud' else provider
        raw = str(item.get('id') or '')
        for prefix in (provider + ':', 'soundcloud:' if provider == 'sc' else provider + ':'):
            while raw.startswith(prefix):
                raw = raw[len(prefix):]
        if provider not in ('sc', 'deezer') or not raw or ':' in raw:
            raise SourceError('source_identity_invalid')
        identity = provider + ':' + raw
        if identity in identities:
            raise SourceError('source_duplicate_identity')
        identities.add(identity)
        try:
            duration = float(item.get('duration', item.get('duration_expected', 0)) or 0)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise SourceError('source_incomplete') from None
        clean = dict(id=raw, provider=provider, title=str(item.get('title') or ''),
                     artist=str(item.get('artist') or ''), album=str(item.get('album') or ''), duration=duration)
        if 'url' in item:
            if not isinstance(item['url'], str):
                raise SourceError('source_incomplete')
            clean['url'] = item['url']
        result.append(clean)
    return result


def _local_sidecar(directory):
    """Never turn missing/corrupt persisted membership into an empty deletion."""
    path = library.sidecar_path(directory)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        library._validate_sidecar(data)
        return data
    except (ValueError, OSError, TypeError):
        raise SourceError('local_membership_unavailable') from None


def local_tracks(directory):
    data = _local_sidecar(directory)
    if 'local_membership' in data:
        return canonical_tracks(data['local_membership'])
    # Legacy local playlists retain every sidecar member, including failed jobs.
    entries = []
    try:
        for key, item in sorted(data['tracks'].items(), key=lambda pair: int(pair[1].get('position') or 0)):
            entries.append({**item, 'id': key, 'provider': item.get('provider') or ('sc' if key.startswith('sc:') else 'deezer')})
    except (ValueError, TypeError, KeyError):
        raise SourceError('local_membership_unavailable') from None
    return canonical_tracks(entries)


def persist_local_membership(directory, requested, *, mode='append'):
    operational = requested
    requested = canonical_tracks(requested)
    with file_lock(library.sidecar_path(directory)):
        if library.sidecar_path(directory).exists():
            data = _local_sidecar(directory)
            previous = local_tracks(directory)
        elif library.sidecar_path(directory).with_suffix('.json.bak').exists():
            raise SourceError('local_membership_unavailable')
        else:
            data, previous = {'tracks': {}}, []
        if mode in ('append', 'playlist_order'):
            # Refresh transport URLs on an explicit re-add, without changing
            # retained order/title/file/status metadata (including legacy rows).
            urls = {(t['provider'], t['id']): t['url'] for t in requested if 'url' in t}
            previous = [{**t, 'url': urls[(t['provider'], t['id'])]} if (t['provider'], t['id']) in urls else t
                        for t in previous]
            existing = {(t['provider'], t['id']) for t in previous}
            members = previous + [t for t in requested if (t['provider'], t['id']) not in existing]
        elif mode == 'replace_membership':
            members = requested
        else:
            raise SourceError('local_membership_mode_invalid')
        # File/status metadata is not replaced by a cross-folder projection.
        data['local_membership'] = members
        library.save_sidecar(directory, data)
    # Membership is a durable projection, not the downloader's transport payload.
    # Keep operational fields such as URL/position/total while canonical IDs and
    # metadata take precedence consistently in both producer and queue consumer.
    return [{**raw, **clean} for raw, clean in zip(operational, requested)]


def source_tracks(key, title):
    from . import main
    if key.startswith('local:'):
        source = next((s for s in main._local_sources() if str(s['id']) == key[6:]), None)
        if not source:
            raise SourceError('source_unknown')
        directory = library.playlist_dir(key, source['title'])
        return directory, local_tracks(directory)
    if key.startswith('sc:'):
        source = next((s for s in main._sc_sources() if str(s['id']) == key[3:]), None)
        if not source:
            raise SourceError('source_unknown')
        try:
            data = main.soundcloud.resolve(source['url'], use_cache=False)
        except Exception:
            raise SourceError('source_unavailable') from None
        if not isinstance(data, dict) or data.get('errors') or data.get('error') or data.get('complete') is False:
            raise SourceError('source_incomplete')
        tracks = canonical_tracks(data.get('tracks'), 'sc')
        for field in ('total', 'track_count', 'playlist_count'):
            if field in data and (type(data[field]) is not int or data[field] != len(tracks)):
                raise SourceError('source_incomplete')
        return library.playlist_dir(key, source['title']), tracks
    if not key.isdecimal():
        raise SourceError('source_unknown')
    try:
        tracks = main.fetch_tracks(key, force_refresh=True)
    except Exception:
        raise SourceError('source_unavailable') from None
    return library.playlist_dir(key, title), canonical_tracks(tracks)


def resolve(key, title):
    directory, requested = source_tracks(key, title)
    rows = catalog_service.playlist_tracks(directory, requested, require_complete=True)
    unresolved = [{'code': 'catalog_' + row['status'], 'provider_id': row['provider'] + ':' + row['id'],
                   'position': i, 'status': row['status'], 'locations': row.get('locations', [])}
                  for i, row in enumerate(rows, 1) if row['status'] != 'ok' or not row.get('file_path')]
    if unresolved:
        raise SourceError('source_unresolved', unresolved)
    return [dict(provider_id=row['provider'] + ':' + row['id'], title=row['title'], artist=row['artist'],
                 album=row['album'], duration=int(row['duration']), position=i, path=row['file_path'])
            for i, row in enumerate(rows, 1)]


def blocked(exc, dry_run=True):
    code = getattr(exc, 'code', 'source_unavailable')
    unresolved = getattr(exc, 'unresolved', [{'code': code}])
    record('warning', operation='rekordbox', stage='source_blocked',
           message='Rekordbox source or verified media is unavailable', error_code=code)
    return dict(dry_run=dry_run, applied=False, reconciled=False, unchanged=False, plan=None,
                unresolved=unresolved, backup_id=None, backup=None, recovery=False, error={'code': code, 'message': code})


def _media_desired(desired, store, *, to_wav=False, require_prepared=False):
    result = []
    for item in desired:
        entry = store.entry(item['path'])
        if not entry:
            if require_prepared:
                raise MediaError('wav_not_prepared')
            result.append(dict(item))
            continue
        proof = store.verify(item['path'])
        result.append({**item, 'path': proof['variant'] if to_wav else proof['source'],
                       'source_path': proof['source'] if to_wav else proof['variant'],
                       'verified_path_sha256': proof['variant_sha256'] if to_wav else proof['source_sha256'],
                       'verified_source_sha256': proof['source_sha256'] if to_wav else proof['variant_sha256']})
    return result


def media_state_from_plan(desired, plan, store):
    current = (plan or {}).get('current_memberships', [])
    rows, modes = [], set()
    for item in desired:
        original = item['path']
        entry = store.entry(original)
        allowed = {path_key(original): 'original'}
        if entry:
            store.verify(original)
            allowed[path_key(entry['variant'])] = 'wav'
        matches = [row for row in current if path_key(row['path']) in allowed]
        mode = allowed[path_key(matches[0]['path'])] if len(matches) == 1 else ('not_in_rekordbox' if not matches else 'blocked')
        if mode != 'not_in_rekordbox':
            modes.add(mode)
        rows.append(dict(provider_id=item['provider_id'], original_path=original,
                         wav_path=entry['variant'] if entry else None, prepared=bool(entry), mode=mode,
                         current_path=matches[0]['path'] if len(matches) == 1 else None,
                         content_id=matches[0].get('content_id') if len(matches) == 1 else None))
    known = {path_key(row['current_path']) for row in rows if row['current_path']}
    unmatched = [row for row in current if path_key(row['path']) not in known]
    target_exists = (plan or {}).get('target', {}).get('id') is not None
    missing = sum(row['mode'] == 'not_in_rekordbox' for row in rows)
    membership = 'absent_target' if not target_exists else ('partial' if missing else 'complete')
    mode = next(iter(modes)) if len(modes) == 1 else ('empty' if not modes else 'mixed')
    if 'blocked' in modes:
        mode = 'blocked'
        membership = 'mismatch'
    return dict(mode=mode, tracks=rows, unmatched_memberships=unmatched,
                membership=membership, target_exists=target_exists, missing=missing,
                preserved=len(unmatched),
                prepared=sum(row['prepared'] for row in rows), total=len(rows),
                shared_content=(plan or {}).get('shared_content', []))


def inspect_state(key, title, playlist_id=None):
    pending = pending_recovery()
    if pending:
        return {**pending, 'media_state': {'mode': 'blocked'}}
    try:
        desired, store = resolve(key, title), MediaStore()
        media_desired = _media_desired(desired, store)
        result = rb.sync_playlist(title, media_desired, dry_run=True, playlist_id=playlist_id, log_activity=False)
        if result.get('error') or result.get('unresolved'):
            return {**result, 'media_state': {'mode': 'blocked'}}
        return {**result, 'media_state': media_state_from_plan(desired, result['plan'], store)}
    except (SourceError, MediaError, MusicRootRequired, catalog_service.LibraryUnavailable) as exc:
        return {**blocked(exc), 'media_state': {'mode': 'blocked'}}


def sync(key, title, *, dry_run=True, expected_plan_hash=None, confirmation_token=None, playlist_id=None, to_wav=None):
    # Cheap authorization before source/provider/adapter access.
    if not dry_run and (confirmation_token != rb.APPLY_CONFIRMATION_TOKEN or not expected_plan_hash):
        return rb.sync_playlist(title, [], dry_run=False, expected_plan_hash=expected_plan_hash,
                                confirmation_token=confirmation_token, playlist_id=playlist_id)
    pending = pending_recovery(dry_run=dry_run)
    if pending:
        return pending
    try:
        desired, store = resolve(key, title), MediaStore()
        media_desired = _media_desired(desired, store, to_wav=bool(to_wav), require_prepared=to_wav is True)
        if to_wav is None and any(store.entry(item['path']) for item in desired):
            # Ordinary sync retains each existing content's actual mode.
            observed = rb.sync_playlist(title, media_desired, dry_run=True, playlist_id=playlist_id, log_activity=False)
            if observed.get('error'):
                return {**observed, 'dry_run': dry_run}
            # existing_path is resolved against the whole collection ContentID,
            # including items not yet in this target. Target memberships alone
            # cannot tell whether adding a shared track would relocate it globally.
            existing_paths = {row['provider_id']: row.get('existing_path')
                              for row in observed['plan'].get('desired_resolved', []) if row.get('content_id')}
            media_desired = [(_media_desired([item], store, to_wav=True)[0]
                              if store.entry(item['path']) and existing_paths.get(item['provider_id'])
                              and path_key(store.entry(item['path'])['variant']) == path_key(existing_paths[item['provider_id']]) else candidate)
                             for item, candidate in zip(desired, media_desired)]
        result = rb.sync_playlist(title, media_desired, dry_run=dry_run, expected_plan_hash=expected_plan_hash,
                                 confirmation_token=confirmation_token, playlist_id=playlist_id,
                                 operation_kind='sync' if to_wav is None else 'relocate')
        try:
            result['media_state'] = _result_media_state(result, desired, store, title, media_desired, playlist_id, dry_run)
        except Exception:
            # Bookkeeping/inspection can fail AFTER a correct commit. Preserve
            # the core's applied/reconciled/error truth and expose blocked mode.
            result['media_state'] = {'mode': 'blocked', 'error': {'code': 'media_state_unavailable'}}
        return result
    except (SourceError, MediaError, MusicRootRequired, catalog_service.LibraryUnavailable) as exc:
        return blocked(exc, dry_run)


def pending_recovery(*, dry_run=True):
    status = rb.get_recovery_status()
    if status['needed']:
        return rb._failure((status.get('error') or {}).get('code', 'recovery_needed'), plan={}, dry_run=dry_run)
    return None


def _result_media_state(result, desired, store, title, media_desired, playlist_id, dry_run):
    if not dry_run and result.get('reconciled'):
        # The map precedes DB commit; actual state is read back, never guessed
        # from a callback or from the pre-operation plan returned by the core.
        fresh = rb.sync_playlist(title, media_desired, dry_run=True, playlist_id=playlist_id, log_activity=False)
        if fresh.get('error') or fresh.get('unresolved'):
            return {'mode': 'blocked', 'error': fresh.get('error')}
        return media_state_from_plan(desired, fresh['plan'], store)
    if result.get('error') or result.get('unresolved'):
        return {'mode': 'blocked'}
    if result.get('plan'):
        return media_state_from_plan(desired, result['plan'], store)
    return {'mode': 'blocked'}


def prepare_wav(key, title, bit_depth=16):
    result = dict(prepared=0, reused=0, total=0, tracks=[], error=None, unresolved=[])
    record('info', operation='rekordbox', stage='wav_preparation_started',
           message='Verified WAV preparation started')
    try:
        desired, store = resolve(key, title), MediaStore()
        result['total'] = len(desired)
        for item in desired:
            entry, created = store.prepare(item['path'], bit_depth)
            result['prepared' if created else 'reused'] += 1
            result['tracks'].append(dict(provider_id=item['provider_id'], original_path=entry['source'], wav_path=entry['variant'], state='prepared'))
        result['state'] = 'prepared'
        record('info', operation='rekordbox', stage='wav_prepared',
               message='Verified WAV files are ready')
    except (SourceError, MediaError, MusicRootRequired, catalog_service.LibraryUnavailable) as exc:
        result.update(state='blocked', error={'code': exc.code, 'message': str(exc)},
                      unresolved=getattr(exc, 'unresolved', [{'code': exc.code}]))
    except Exception:
        result.update(state='blocked', error={'code': 'wav_preparation_failed', 'message': 'WAV preparation failed'})
    if result['error']:
        record('warning', operation='rekordbox', stage='wav_preparation_failed',
               message='Verified WAV preparation could not finish', error_code=result['error']['code'])
    return result
