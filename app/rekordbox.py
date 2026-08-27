# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

APPLY_CONFIRMATION_TOKEN = "APPLY_REKORDBOX_CHANGES"
SUPPORTED_PYREKORDBOX_VERSION = "0.4.4"
CANONICAL_FIELDS = ("provider_id", "title", "artist", "album", "duration", "position", "path")


def db_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox" / "master.db"


def db_exists() -> bool:
    return db_path().exists()


def rb_running() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"], capture_output=True, text=True)
    return "rekordbox.exe" in r.stdout.lower()


def open_db():
    return _PyrekordboxAdapter().db


def backup_db() -> Path:
    src = db_path()
    dst = src.with_name(f"master.db.deckpipe-backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}")
    shutil.copy2(src, dst)
    return dst


def get_rb_playlists() -> list:
    adapter = _PyrekordboxAdapter()
    try:
        return adapter.list_playlists()
    finally:
        adapter.close()


def _canonical_track(item: dict, *, validate_path: bool) -> tuple[dict, dict | None]:
    clean = {field: item.get(field, "") for field in CANONICAL_FIELDS}
    clean["provider_id"] = str(clean["provider_id"])
    clean["title"] = str(clean["title"])
    clean["artist"] = str(clean["artist"])
    clean["album"] = str(clean["album"])
    clean["duration"] = int(clean["duration"] or 0)
    clean["position"] = int(clean["position"] or 0)
    clean["path"] = str(clean["path"])
    if not clean["provider_id"]:
        return clean, {"code": "missing_provider_identity", "provider_id": "", "position": clean["position"]}
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

    desired_index, desired_dupes, desired_unresolved = _index_unique(desired_clean, "duplicate_desired_identity")
    current_index, current_dupes, current_unresolved = _index_unique(current_clean, "ambiguous_current_identity")
    unresolved.extend(desired_unresolved)
    unresolved.extend(current_unresolved)
    invalid_desired = {
        item["provider_id"]
        for item in unresolved
        if item["code"] in {"relative_desired_path", "missing_desired_path", "missing_provider_identity", "duplicate_desired_identity"}
    }
    invalid_current = {
        item["provider_id"]
        for item in unresolved
        if item["code"] in {"ambiguous_current_identity", "missing_provider_identity"}
    }
    desired_resolved = [
        item for item in desired_clean
        if item["provider_id"] in desired_index and item["provider_id"] not in invalid_desired and item["provider_id"] not in current_dupes
    ]
    current_resolved = [
        item for item in current_clean
        if item["provider_id"] in current_index and item["provider_id"] not in invalid_current and item["provider_id"] not in desired_dupes
    ]
    desired_by_id = {item["provider_id"]: item for item in desired_resolved}
    current_by_id = {item["provider_id"]: item for item in current_resolved}
    common = sorted(desired_by_id.keys() & current_by_id.keys(), key=lambda key: desired_by_id[key]["position"])
    add = [item for item in desired_resolved if item["provider_id"] not in current_by_id]
    remove = [item for item in current_resolved if item["provider_id"] not in desired_by_id]
    reorder = [desired_by_id[key] for key in common if desired_by_id[key]["position"] != current_by_id[key]["position"]]
    metadata = [
        desired_by_id[key]
        for key in common
        if any(desired_by_id[key][field] != current_by_id[key][field] for field in ("title", "artist", "album", "duration"))
    ]
    path = [desired_by_id[key] for key in common if desired_by_id[key]["path"] != current_by_id[key]["path"]]
    unresolved = sorted(unresolved, key=lambda item: (item.get("code", ""), item.get("provider_id", ""), item.get("position", 0)))
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


@contextmanager
def _exclusive_create_lock(lock_path: Path) -> Iterator[bool]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield True
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _copy_verified(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if src.read_bytes() != dst.read_bytes():
        raise RuntimeError("backup verification failed")


def _create_backup(adapter, playlist_name: str, desired: list[dict]) -> dict:
    backup_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex
    root = Path(adapter.db_path).parent / "deckpipe-rekordbox-backups" / backup_id
    root.mkdir(parents=True, exist_ok=False)
    db_backup = root / "master.db"
    _copy_verified(Path(adapter.db_path), db_backup)
    external_items = []
    inventory_names = ["master.db"]
    missing = []
    seen = {str(Path(adapter.db_path).resolve())}
    for original in adapter.external_files_for_playlist(playlist_name, desired):
        original = Path(original)
        key = str(original.resolve()) if original.exists() else str(original.absolute())
        if key in seen:
            continue
        seen.add(key)
        existed = original.exists()
        backup = root / f"external-{len(external_items):03d}-{original.name}"
        if existed:
            _copy_verified(original, backup)
        else:
            missing.append(original.name)
        external_items.append({"path": str(original), "backup": str(backup), "existed": existed})
        inventory_names.append(original.name)
    return {
        "id": backup_id,
        "root": str(root),
        "files": {"database": {"path": str(adapter.db_path), "backup": str(db_backup), "existed": True}, "external": external_items},
        "summary": {"inventory_names": inventory_names, "originally_missing": missing, "verified": True},
    }


def _public_plan(plan: dict) -> dict:
    public = json.loads(json.dumps(plan, ensure_ascii=False))
    for key in ("add", "remove", "reorder", "metadata", "path", "desired_resolved"):
        for item in public.get(key, []):
            if "path" in item:
                item["path"] = "<redacted>"
    return public


def _failure(code: str, *, plan: dict, backup: dict | None = None) -> dict:
    return {
        "dry_run": False,
        "applied": False,
        "reconciled": False,
        "unresolved": plan.get("unresolved", []),
        "backup_id": backup.get("id") if backup else None,
        "backup": backup.get("summary") if backup else None,
        "plan": _public_plan(plan),
        "error": {"code": code, "message": "Rekordbox sync failed"},
    }


def _is_exact_reconciled(desired_resolved: list[dict], reopened_snapshot: list[dict]) -> bool:
    reopened_plan = plan_playlist_sync(desired_resolved, reopened_snapshot)
    return (
        reopened_plan["counts"]["unresolved"] == 0
        and reopened_plan["counts"]["add"] == 0
        and reopened_plan["counts"]["remove"] == 0
        and reopened_plan["counts"]["reorder"] == 0
        and reopened_plan["counts"]["metadata"] == 0
        and reopened_plan["counts"]["path"] == 0
    )


def sync_playlist(
    pl_name: str,
    ordered_files: list,
    create_missing: bool = True,
    *,
    dry_run: bool = True,
    adapter_factory: Callable[[], object] | None = None,
    confirmation_token: str | None = None,
    on_reconciled: Callable[[dict], None] | None = None,
) -> dict:
    del create_missing
    adapter_factory = adapter_factory or _PyrekordboxAdapter
    adapter = adapter_factory()
    current = adapter.snapshot_playlist(pl_name)
    plan = plan_playlist_sync(ordered_files, current)
    if dry_run:
        return {"dry_run": True, "applied": False, "reconciled": False, "unresolved": plan["unresolved"], "backup_id": None, "backup": None, "plan": plan, "error": None}
    if confirmation_token != APPLY_CONFIRMATION_TOKEN:
        return _failure("apply_not_confirmed", plan=plan)
    if plan["unresolved"]:
        return _failure("unresolved_items", plan=plan)
    if adapter.is_rekordbox_running():
        return _failure("rekordbox_running", plan=plan)

    with _exclusive_create_lock(adapter.mutation_lock_path()) as acquired:
        if not acquired:
            return _failure("concurrent_apply", plan=plan)
        backup = None
        closed = False
        try:
            backup = _create_backup(adapter, pl_name, plan["desired_resolved"])
            adapter.begin()
            try:
                adapter.apply_operations(plan)
            except Exception:
                raise _SyncFailure("adapter_apply_failed") from None
            try:
                adapter.save_external_files()
            except Exception:
                raise _SyncFailure("external_save_failed") from None
            try:
                adapter.commit()
            except Exception:
                raise _SyncFailure("commit_failed") from None
            adapter.close()
            closed = True
            try:
                reopened = adapter.reopen()
                reopened_snapshot = reopened.snapshot_playlist(pl_name)
            except Exception:
                raise _SyncFailure("reopen_failed") from None
            if not _is_exact_reconciled(plan["desired_resolved"], reopened_snapshot):
                raise _SyncFailure("reconcile_failed") from None
            result = {
                "dry_run": False,
                "applied": True,
                "reconciled": True,
                "unresolved": [],
                "backup_id": backup["id"],
                "backup": backup["summary"],
                "plan": plan,
                "plan_hash": plan["hash"],
                "error": None,
            }
            if on_reconciled:
                on_reconciled(result)
            return result
        except _SyncFailure as exc:
            try:
                adapter.rollback()
            except Exception:
                pass
            if backup is not None:
                try:
                    adapter.restore_from_backup(backup)
                except Exception:
                    return _failure("rollback_verify_failed", plan=plan, backup=backup)
            try:
                restored = adapter.snapshot_playlist(pl_name)
                if not _is_exact_reconciled(current, restored):
                    return _failure("rollback_verify_failed", plan=plan, backup=backup)
            except Exception:
                return _failure("rollback_verify_failed", plan=plan, backup=backup)
            return _failure(exc.code, plan=plan, backup=backup)
        finally:
            if not closed:
                try:
                    adapter.close()
                except Exception:
                    pass


class _SyncFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _PyrekordboxAdapter:
    def __init__(self) -> None:
        self._assert_supported_pyrekordbox()
        import pyrekordbox.config as cfg
        from pyrekordbox import Rekordbox6Database

        self.db_path = db_path()
        cfg.__config__["rekordbox7"] = {"db_path": self.db_path}
        self.db = Rekordbox6Database(path=self.db_path)

    def _assert_supported_pyrekordbox(self) -> None:
        import pyrekordbox

        version = getattr(pyrekordbox, "__version__", SUPPORTED_PYREKORDBOX_VERSION)
        if str(version) != SUPPORTED_PYREKORDBOX_VERSION:
            raise RuntimeError("unsupported pyrekordbox version")

    def mutation_lock_path(self) -> Path:
        return self.db_path.with_name("deckpipe-rekordbox-mutation.lock")

    def is_rekordbox_running(self) -> bool:
        return rb_running()

    def list_playlists(self) -> list:
        return [{"id": str(p.ID), "name": p.Name, "count": len(p.Songs)} for p in self.db.get_playlist() if p.Attribute == 0]

    def snapshot_playlist(self, playlist_name: str) -> list[dict]:
        contents = {}
        for content in self.db.get_content():
            provider_id = str(getattr(content, "Comments", "") or getattr(content, "ID", ""))
            contents[str(getattr(content, "ID", ""))] = {
                "provider_id": provider_id,
                "title": str(getattr(content, "Title", "") or ""),
                "artist": str(getattr(getattr(content, "Artist", None), "Name", "") or getattr(content, "Artist", "") or ""),
                "album": str(getattr(getattr(content, "Album", None), "Name", "") or getattr(content, "Album", "") or ""),
                "duration": int(getattr(content, "Length", 0) or 0),
                "position": 0,
                "path": str(getattr(content, "FolderPath", "") or ""),
            }
        target = None
        for playlist in self.db.get_playlist():
            if playlist.Attribute == 0 and str(playlist.Name) == playlist_name:
                target = playlist
                break
        if target is None:
            return []
        out = []
        for position, song in enumerate(self.db.get_playlist_songs(PlaylistID=target.ID), start=1):
            item = dict(contents.get(str(song.ContentID), {}))
            if item:
                item["position"] = position
                out.append(item)
        return out

    def external_files_for_playlist(self, _playlist_name: str, desired: list[dict]) -> list[Path]:
        root = self.db_path.parent
        files = [root / "masterPlaylists6.xml"]
        for item in desired:
            path = Path(item["path"])
            files.extend(path.parent.glob("*.DAT"))
            files.extend(path.parent.glob("*.EXT"))
        return files

    def begin(self) -> None:
        pass

    def apply_operations(self, _plan: dict) -> None:
        raise RuntimeError("real Rekordbox apply requires disposable-copy validation")

    def save_external_files(self) -> None:
        pass

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def close(self) -> None:
        self.db.close()

    def reopen(self):
        return _PyrekordboxAdapter()

    def restore_from_backup(self, backup: dict) -> None:
        shutil.copy2(backup["files"]["database"]["backup"], backup["files"]["database"]["path"])
        for item in backup["files"]["external"]:
            original = Path(item["path"])
            if item["existed"]:
                shutil.copy2(item["backup"], original)
            else:
                original.unlink(missing_ok=True)
