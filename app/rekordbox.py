# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

APPLY_CONFIRMATION_TOKEN = "APPLY_REKORDBOX_CHANGES"
SUPPORTED_PYREKORDBOX_VERSION = "0.4.4"
CANONICAL_FIELDS = ("provider_id", "title", "artist", "album", "duration", "position", "path", "source_path", "content_id", "existing_path", "verified_path_sha256", "verified_source_sha256")


def db_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox" / "master.db"


def db_exists() -> bool:
    return db_path().exists()


def rb_running() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"], capture_output=True, text=True)
    return "rekordbox.exe" in r.stdout.lower()


def open_db():
    adapter = _PyrekordboxAdapter()
    original_close = adapter.db.close
    def close_owned_handle():
        adapter.db.close = original_close
        adapter.close()
    adapter.db.close = close_owned_handle
    return adapter.db


def backup_db() -> Path:
    adapter = _PyrekordboxAdapter()
    try:
        dst = adapter.db_path.with_name(f"master.db.deckpipe-backup-{uuid.uuid4().hex}")
        adapter.backup_database(dst)
        return dst
    finally:
        adapter.close()


def get_rb_playlists() -> list:
    adapter = _PyrekordboxAdapter()
    try:
        adapter.read_only()
        return adapter.list_playlists()
    finally:
        adapter.close()


def _canonical_track(item: dict, *, validate_path: bool) -> tuple[dict, dict | None]:
    clean = {field: item.get(field, "") for field in CANONICAL_FIELDS}
    clean["provider_id"] = str(clean["provider_id"])
    for prefix in ("soundcloud:", "deezer:", "sc:"):
        while clean["provider_id"].startswith(prefix + prefix):
            clean["provider_id"] = clean["provider_id"][len(prefix):]
    clean["title"] = str(clean["title"])
    clean["artist"] = str(clean["artist"])
    clean["album"] = str(clean["album"])
    clean["duration"] = int(clean["duration"] or 0)
    clean["position"] = int(clean["position"] or 0)
    clean["path"] = str(clean["path"])
    if not clean["provider_id"]:
        return clean, {"code": "missing_provider_identity", "provider_id": "", "position": clean["position"]}
    if validate_path and clean["position"] <= 0:
        return clean, {"code": "nonpositive_desired_position", "provider_id": clean["provider_id"], "position": clean["position"]}
    path = Path(clean["path"])
    if validate_path and not path.is_absolute():
        return clean, {"code": "relative_desired_path", "provider_id": clean["provider_id"], "position": clean["position"]}
    if validate_path and not path.is_file():
        return clean, {"code": "missing_desired_path", "provider_id": clean["provider_id"], "position": clean["position"]}
    return clean, None


def _index_unique(items: list[dict], duplicate_code: str) -> tuple[dict[str, dict], set[str], list[dict]]:
    by_id: dict[str, list[dict]] = {}
    for item in items:
        by_id.setdefault(item["provider_id"], []).append(item)
    unique = {key: values[0] for key, values in by_id.items() if len(values) == 1}
    duplicates = {key for key, values in by_id.items() if len(values) > 1}
    unresolved = [{"code": duplicate_code, "provider_id": key, "count": len(by_id[key])} for key in sorted(duplicates)]
    return unique, duplicates, unresolved


def _duplicate_desired_positions(items: list[dict]) -> tuple[set[int], list[dict]]:
    by_position: dict[int, list[dict]] = {}
    for item in items:
        if item["position"] > 0:
            by_position.setdefault(item["position"], []).append(item)
    duplicates = {position for position, values in by_position.items() if len(values) > 1}
    unresolved = [
        {
            "code": "duplicate_desired_position",
            "position": position,
            "provider_ids": [item["provider_id"] for item in sorted(by_position[position], key=lambda value: value["provider_id"])],
        }
        for position in sorted(duplicates)
    ]
    return duplicates, unresolved


def plan_playlist_sync(desired: list[dict], current: list[dict] | None = None) -> dict:
    desired_clean: list[dict] = []
    current_clean: list[dict] = []
    unresolved: list[dict] = []
    for item in desired:
        clean, problem = _canonical_track(dict(item), validate_path=True)
        desired_clean.append(clean)
        if problem:
            unresolved.append(problem)
    for item in current or []:
        clean, problem = _canonical_track(dict(item), validate_path=False)
        current_clean.append(clean)
        if problem:
            unresolved.append(problem)

    desired_position_dupes, desired_position_unresolved = _duplicate_desired_positions(desired_clean)
    desired_index, desired_dupes, desired_unresolved = _index_unique(desired_clean, "duplicate_desired_identity")
    current_index, current_dupes, current_unresolved = _index_unique(current_clean, "ambiguous_current_identity")
    unresolved.extend(desired_position_unresolved)
    unresolved.extend(desired_unresolved)
    unresolved.extend(current_unresolved)
    invalid_desired = {
        item["provider_id"]
        for item in unresolved
        if item["code"] in {"relative_desired_path", "missing_desired_path", "missing_provider_identity", "nonpositive_desired_position", "duplicate_desired_identity"}
    }
    invalid_desired.update(
        item["provider_id"]
        for item in desired_clean
        if item["position"] in desired_position_dupes
    )
    invalid_current = {
        item["provider_id"]
        for item in unresolved
        if item["code"] in {"ambiguous_current_identity", "missing_provider_identity"}
    }
    desired_resolved = sorted([
        item for item in desired_clean
        if item["provider_id"] in desired_index and item["provider_id"] not in invalid_desired and item["provider_id"] not in current_dupes
    ], key=lambda item: (item["position"], item["provider_id"]))
    current_resolved = sorted([
        item for item in current_clean
        if item["provider_id"] in current_index and item["provider_id"] not in invalid_current and item["provider_id"] not in desired_dupes
    ], key=lambda item: (item["position"], item["provider_id"]))
    desired_by_id = {item["provider_id"]: item for item in desired_resolved}
    current_by_id = {item["provider_id"]: item for item in current_resolved}
    common = sorted(desired_by_id.keys() & current_by_id.keys(), key=lambda key: (desired_by_id[key]["position"], key))
    add = sorted([item for item in desired_resolved if item["provider_id"] not in current_by_id], key=lambda item: (item["position"], item["provider_id"]))
    remove = sorted([item for item in current_resolved if item["provider_id"] not in desired_by_id], key=lambda item: (item["position"], item["provider_id"]))
    reorder = [desired_by_id[key] for key in common if desired_by_id[key]["position"] != current_by_id[key]["position"]]
    metadata = [
        desired_by_id[key]
        for key in common
        if any(desired_by_id[key][field] != current_by_id[key][field] for field in ("title", "artist", "album", "duration"))
    ]
    path = [desired_by_id[key] for key in common if desired_by_id[key]["path"] != current_by_id[key]["path"]]
    unresolved = sorted(unresolved, key=lambda item: (item.get("code", ""), item.get("position", 0), item.get("provider_id", ""), ",".join(item.get("provider_ids", []))))
    plan = {
        "add": add,
        "remove": remove,
        "reorder": reorder,
        "metadata": metadata,
        "path": path,
        "unresolved": unresolved,
        "desired_resolved": desired_resolved,
        "counts": {
            "desired": len(desired_clean),
            "current": len(current_clean),
            "resolved": len(desired_resolved),
            "add": len(add),
            "remove": len(remove),
            "reorder": len(reorder),
            "metadata": len(metadata),
            "path": len(path),
            "unresolved": len(unresolved),
        },
    }
    plan["hash"] = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return plan


from .rekordbox_adapter import PyrekordboxAdapter as _PyrekordboxAdapter, AdapterError, canonical_path
from .rekordbox_recovery import Journal, RecoveryError, digest, mutation_lock

_exclusive_create_lock = mutation_lock


def _failure(code, *, plan, dry_run=False, backup=None):
    return {'dry_run': dry_run, 'applied': False, 'reconciled': False,
        'unchanged': False, 'unresolved': plan.get('unresolved', []), 'plan': plan,
        'backup_id': backup.get('id') if backup else None,
        'backup': None, 'recovery': code.startswith('recovery_') or code == 'rollback_verify_failed',
        'error': {'code': code, 'message': 'Rekordbox operation could not be completed'}}


def _bound_plan(adapter, name, desired, playlist_id, operation_kind):
    canonical = [_canonical_track(dict(i), validate_path=True)[0] for i in desired]
    resolved, current, context = adapter.context(name, canonical, playlist_id, operation_kind)
    plan = plan_playlist_sync(resolved, current)
    plan.update(context)
    # Existing metadata is owned by Rekordbox; source metadata only initializes
    # new content. It must never cause writes or make a no-op appear different.
    plan['metadata'] = []
    plan['counts']['metadata'] = 0
    plan['path'] = [i for i in plan['desired_resolved'] if i.get('existing_path') and
        canonical_path(i['existing_path']) != canonical_path(i['path'])]
    plan['counts']['path'] = len(plan['path'])
    seen_paths = set()
    for item in plan['desired_resolved']:
        path = canonical_path(item['path'])
        if path in seen_paths:
            plan['unresolved'].append({'code': 'duplicate_desired_path', 'provider_id': item['provider_id']})
        seen_paths.add(path)
        if item.get('source_path') and not Path(item['source_path']).is_file():
            plan['unresolved'].append({'code': 'missing_source_path', 'provider_id': item['provider_id']})
        changing_path = item in plan['path']
        for path_field, expected_field in (('path', 'verified_path_sha256'), ('source_path', 'verified_source_sha256')):
            expected = item.get(expected_field)
            if changing_path and not expected:
                plan['unresolved'].append({'code': 'media_verification_required', 'provider_id': item['provider_id']})
            elif expected and context['media_fingerprints'].get(item.get(path_field)) != expected:
                plan['unresolved'].append({'code': 'media_verification_changed', 'provider_id': item['provider_id']})
    if operation_kind == 'relocate' and (plan['add'] or plan['remove'] or plan['reorder'] or not plan['target']['id']):
        plan['unresolved'].append({'code': 'relocate_membership_change'})
    plan['counts']['unresolved'] = len(plan['unresolved'])
    plan['shared_content'] = []
    if plan['path']:
        for item in plan['path']:
            playlists = sorted({str(s.PlaylistID) for s in adapter.db.get_playlist_songs(ContentID=item['content_id'])})
            plan['shared_content'].append({'content_id': item['content_id'], 'playlist_ids': playlists})
    plan['version'] = 2
    plan.pop('hash', None)
    plan['hash'] = digest(plan)
    return plan


def _no_changes(plan):
    return not plan['unresolved'] and plan['target']['id'] is not None and not any(
        plan[k] for k in ('add', 'remove', 'reorder', 'path'))


def sync_playlist(pl_name, ordered_files, create_missing=True, *, dry_run=True,
        adapter_factory=None, confirmation_token=None, expected_plan_hash=None,
        playlist_id=None, operation_kind='sync', on_reconciled=None):
    """Preview or apply exactly the viewed ordered playlist using a durable journal.

    source_path is a verified alias supplied by the media service. Public callers
    must never forward unverified client file paths into this internal interface.
    """
    plan = plan_playlist_sync(ordered_files, [])
    if operation_kind not in ('sync', 'relocate'):
        return _failure('invalid_operation_kind', plan=plan, dry_run=dry_run)
    if not dry_run and confirmation_token != APPLY_CONFIRMATION_TOKEN:
        return _failure('apply_not_confirmed', plan=plan)
    if not dry_run and not expected_plan_hash:
        return _failure('preview_required', plan=plan)
    factory = adapter_factory or _PyrekordboxAdapter
    adapter = None
    journal = None
    try:
        adapter = factory()
        journal = Journal(adapter.db_path)
        if dry_run:
            if journal.pending:
                return _failure('recovery_needed', plan=plan, dry_run=True, backup=journal.data)
            adapter.read_only()
            plan = _bound_plan(adapter, pl_name, ordered_files, playlist_id, operation_kind)
            return {'dry_run': True, 'applied': False, 'reconciled': False,
                'unchanged': _no_changes(plan), 'plan': plan, 'unresolved': plan['unresolved'],
                'backup_id': None, 'backup': None, 'error': None, 'recovery': False}
        lock_path = adapter.mutation_lock_path()
        # Release the read handle before waiting for the process lock.
        adapter.close()

        adapter = None
        with mutation_lock(lock_path) as acquired:
            if not acquired:
                return _failure('concurrent_apply', plan=plan)
            adapter = factory()
            journal = Journal(adapter.db_path)
            if adapter.is_rekordbox_running():
                return _failure('rekordbox_running', plan=plan)
            if journal.pending:
                journal.restore(adapter)
                adapter = None
                return _failure('recovery_restored_preview_required', plan=plan, backup=journal.data)
            adapter.read_only()
            adapter.integrity_check()
            plan = _bound_plan(adapter, pl_name, ordered_files, playlist_id, operation_kind)
            if plan['unresolved']:
                return _failure('unresolved_items', plan=plan)
            # Replay is safe only after proving the authoritative desired state is
            # already exactly present. A stale hash cannot authorize new writes.
            if _no_changes(plan):
                return {'dry_run': False, 'applied': False, 'reconciled': True,
                    'unchanged': True, 'plan': plan, 'plan_hash': plan['hash'],
                    'unresolved': [], 'backup_id': None, 'backup': None, 'error': None, 'recovery': False}
            if expected_plan_hash != plan['hash']:
                return _failure('stale_preview', plan=plan)
            if not create_missing and plan['target']['id'] is None:
                return _failure('playlist_not_found', plan=plan)
            adapter.close()
            adapter = factory()
            adapter.begin()
            locked_plan = _bound_plan(adapter, pl_name, ordered_files, playlist_id, operation_kind)
            if locked_plan['hash'] != expected_plan_hash:
                adapter.rollback()
                return _failure('stale_preview', plan=locked_plan)
            journal.prepare(adapter, plan)
            try:
                if adapter.is_rekordbox_running():
                    raise AdapterError('rekordbox_running')
                locked_plan = _bound_plan(adapter, pl_name, ordered_files, playlist_id, operation_kind)
                if locked_plan['hash'] != expected_plan_hash:
                    adapter.rollback()
                    journal.save(phase='restored')
                    return _failure('stale_preview', plan=locked_plan)
                adapter.apply_operations({**plan, '_create_missing': create_missing})
                payloads = adapter.prepare_commit()
                journal.stage_external(payloads)
                journal.save(phase='commit_pending', database_after=adapter.fingerprint())
                adapter.commit()
                journal.save(phase='database_committed')
                adapter.close()
                adapter = None
                journal.publish()
                adapter = factory()
                adapter.integrity_check()
                if adapter.fingerprint() != journal.data['database_after']:
                    raise AdapterError('reconcile_failed')
                after = _bound_plan(adapter, pl_name, ordered_files, playlist_id, operation_kind)
                if not _no_changes(after):
                    raise AdapterError('reconcile_failed')
                journal.save(phase='complete')
                result = {'dry_run': False, 'applied': True, 'reconciled': True,
                    'unchanged': False, 'plan': plan, 'plan_hash': plan['hash'],
                    'unresolved': [], 'backup_id': journal.data['id'], 'backup': {'verified': True},
                    'error': None, 'recovery': False}
                if on_reconciled:
                    try:
                        on_reconciled(result)
                    except Exception:
                        result['error'] = {'code': 'callback_failed', 'message': 'Media state reconciliation failed'}
                return result
            except Exception as exc:
                code = str(exc) if isinstance(exc, (AdapterError, RecoveryError)) else 'transaction_failed'
                if adapter is not None:
                    try:
                        adapter.rollback()
                    except Exception:
                        code = 'rollback_verify_failed'
                    adapter.close()
                adapter = factory()
                try:
                    journal.restore(adapter)
                    adapter = None
                except Exception as recovery_exc:
                    code = str(recovery_exc) if isinstance(recovery_exc, RecoveryError) else 'rollback_verify_failed'
                return _failure(code, plan=plan, backup=journal.data)
    except (AdapterError, RecoveryError) as exc:
        return _failure(str(exc), plan=plan, dry_run=dry_run, backup=journal.data if journal else None)
    except Exception:
        return _failure('adapter_open_failed' if adapter is None else 'snapshot_failed', plan=plan, dry_run=dry_run)
    finally:
        if adapter is not None:
            adapter.close()




def get_recovery_status(*, database_path=None) -> dict:
    """Read journal status only; never open the database or recover implicitly.

    database_path is an internal fixture injection, never an HTTP parameter.
    """
    try:
        journal = Journal(Path(database_path) if database_path is not None else db_path())
        data = journal.data or {}
        return {'needed': journal.pending, 'phase': data.get('phase'),
            'operation_id': data.get('id'), 'error': None}
    except RecoveryError as exc:
        return {'needed': True, 'phase': None, 'operation_id': None,
            'error': {'code': str(exc)}}
