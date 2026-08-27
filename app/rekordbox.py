# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
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


@contextmanager
def _exclusive_create_lock(lock_path: Path) -> Iterator[bool | None]:
    path = Path(lock_path)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        yield False
        return
    except Exception:
        yield None
        return
    try:
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
    except Exception:
        os.close(fd)
        fd = None
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        yield None
        return
    try:
        yield True
    finally:
        if fd is not None:
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


def _failure(code: str, *, plan: dict, backup: dict | None = None, dry_run: bool = False) -> dict:
    return {
        "dry_run": dry_run,
        "applied": False,
        "reconciled": False,
        "unresolved": plan.get("unresolved", []),
        "backup_id": backup.get("id") if backup else None,
        "backup": backup.get("summary") if backup else None,
        "plan": _public_plan(plan),
        "error": {"code": code, "message": "Rekordbox sync failed"},
    }


def _callback_failure(result: dict) -> dict:
    failed = dict(result)
    failed["plan"] = _public_plan(result.get("plan", {}))
    failed["error"] = {"code": "callback_failed", "message": "Rekordbox sync callback failed"}
    return failed


def _apply_authorized(confirmation_token: str | None) -> bool:
    return os.environ.get("DECKPIPE_RB_EXPERIMENTAL") == "1" and confirmation_token == APPLY_CONFIRMATION_TOKEN


def _close_quietly(handle: object | None) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


def _rollback_quietly(handle: object | None) -> None:
    if handle is None:
        return
    try:
        handle.rollback()
    except Exception:
        pass


def _canonical_snapshot(items: list[dict]) -> list[dict]:
    return [_canonical_track(dict(item), validate_path=False)[0] for item in items]


def _is_exact_snapshot_match(expected_snapshot: list[dict], reopened_snapshot: list[dict]) -> bool:
    return _canonical_snapshot(expected_snapshot) == _canonical_snapshot(reopened_snapshot)


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


def _restore_backup_files(backup: dict) -> None:
    database = backup["files"]["database"]
    db_original = Path(database["path"])
    db_backup = Path(database["backup"])
    shutil.copy2(db_backup, db_original)
    if db_backup.read_bytes() != db_original.read_bytes():
        raise RuntimeError("database restore verification failed")
    for item in backup["files"]["external"]:
        original = Path(item["path"])
        backup_path = Path(item["backup"])
        if item["existed"]:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, original)
            if backup_path.read_bytes() != original.read_bytes():
                raise RuntimeError("external restore verification failed")
        else:
            original.unlink(missing_ok=True)
            if original.exists():
                raise RuntimeError("external absence verification failed")


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
    adapter_factory = adapter_factory or _PyrekordboxAdapter
    plan: dict = plan_playlist_sync(ordered_files, [])
    if not dry_run and not _apply_authorized(confirmation_token):
        return _failure("apply_not_confirmed", plan=plan)
    try:
        adapter = adapter_factory()
    except Exception:
        return _failure("adapter_open_failed", plan=plan, dry_run=dry_run)
    reopened = None
    backup = None
    current: list[dict] = []
    try:
        if dry_run:
            try:
                current = adapter.snapshot_playlist(pl_name)
            except Exception:
                _close_quietly(adapter)
                return _failure("snapshot_failed", plan=plan, dry_run=True)
            plan = plan_playlist_sync(ordered_files, current)
            _close_quietly(adapter)
            return {"dry_run": True, "applied": False, "reconciled": False, "unresolved": plan["unresolved"], "backup_id": None, "backup": None, "plan": plan, "error": None}
        try:
            lock_path = adapter.mutation_lock_path()
        except Exception:
            _close_quietly(adapter)
            return _failure("lock_failed", plan=plan)
        lock_context = _exclusive_create_lock(lock_path)
        try:
            acquired = lock_context.__enter__()
        except Exception:
            _close_quietly(adapter)
            return _failure("lock_failed", plan=plan)
        try:
            if acquired is None:
                _close_quietly(adapter)
                adapter = None
                return _failure("lock_failed", plan=plan)
            if not acquired:
                _close_quietly(adapter)
                adapter = None
                return _failure("concurrent_apply", plan=plan)
            try:
                current = adapter.snapshot_playlist(pl_name)
            except Exception:
                _close_quietly(adapter)
                adapter = None
                return _failure("snapshot_failed", plan=plan)
            plan = plan_playlist_sync(ordered_files, current)
            if plan["unresolved"]:
                _close_quietly(adapter)
                adapter = None
                return _failure("unresolved_items", plan=plan)
            try:
                if adapter.is_rekordbox_running():
                    _close_quietly(adapter)
                    adapter = None
                    return _failure("rekordbox_running", plan=plan)
            except Exception:
                _close_quietly(adapter)
                adapter = None
                return _failure("rekordbox_status_failed", plan=plan)
            playlist_exists = None
            if hasattr(adapter, "playlist_exists"):
                try:
                    playlist_exists = adapter.playlist_exists(pl_name)
                except Exception:
                    _close_quietly(adapter)
                    adapter = None
                    return _failure("playlist_lookup_failed", plan=plan)
            if not create_missing and playlist_exists is False:
                _close_quietly(adapter)
                adapter = None
                return _failure("playlist_not_found", plan=plan)
            operations = {**plan, "_playlist_name": pl_name, "_create_missing": bool(create_missing)}
            transaction_open = False
            committed = False
            try:
                try:
                    backup = _create_backup(adapter, pl_name, plan["desired_resolved"])
                except Exception:
                    _close_quietly(adapter)
                    adapter = None
                    return _failure("backup_failed", plan=plan)
                try:
                    adapter.begin()
                    transaction_open = True
                except Exception:
                    _close_quietly(adapter)
                    adapter = None
                    return _failure("begin_failed", plan=plan, backup=backup)
                try:
                    adapter.apply_operations(operations)
                except Exception:
                    raise _SyncFailure("adapter_apply_failed") from None
                try:
                    adapter.save_external_files()
                except Exception:
                    raise _SyncFailure("external_save_failed") from None
                try:
                    adapter.commit()
                    transaction_open = False
                    committed = True
                except Exception:
                    raise _SyncFailure("commit_failed") from None
                adapter.close()
                adapter = None
                try:
                    reopened = adapter_factory()
                    reopened_snapshot = reopened.snapshot_playlist(pl_name)
                except Exception:
                    raise _SyncFailure("reopen_failed") from None
                if not _is_exact_reconciled(plan["desired_resolved"], reopened_snapshot):
                    raise _SyncFailure("reconcile_failed") from None
                _close_quietly(reopened)
                reopened = None
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
                    try:
                        on_reconciled(result)
                    except Exception:
                        return _callback_failure(result)
                return result
            except _SyncFailure as exc:
                original_code = exc.code
                _close_quietly(reopened)
                reopened = None
                if transaction_open and not committed:
                    _rollback_quietly(adapter)
                _close_quietly(adapter)
                adapter = None
                if backup is not None:
                    try:
                        _restore_backup_files(backup)
                    except Exception:
                        return _failure("rollback_verify_failed", plan=plan, backup=backup)
                try:
                    verifier = adapter_factory()
                    try:
                        restored = verifier.snapshot_playlist(pl_name)
                    finally:
                        _close_quietly(verifier)
                    if not _is_exact_snapshot_match(current, restored):
                        return _failure("rollback_verify_failed", plan=plan, backup=backup)
                except Exception:
                    return _failure("rollback_verify_failed", plan=plan, backup=backup)
                return _failure(original_code, plan=plan, backup=backup)
            finally:
                _close_quietly(reopened)
                _close_quietly(adapter)
        finally:
            lock_context.__exit__(*sys.exc_info())
    except Exception:
        _close_quietly(reopened)
        _close_quietly(adapter)
        raise


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
        self._staged_anlz: dict[Path, object] = {}

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

    def _related_name(self, content, relationship_name: str, fallback_name: str) -> str:
        related = getattr(content, relationship_name, None)
        if related is not None and hasattr(related, "Name"):
            return str(getattr(related, "Name") or "")
        fallback = getattr(content, fallback_name, "")
        return fallback if isinstance(fallback, str) else ""

    def snapshot_playlist(self, playlist_name: str) -> list[dict]:
        contents = {}
        for content in self.db.get_content():
            provider_id = str(getattr(content, "Commnt", "") or getattr(content, "Comments", "") or getattr(content, "ID", ""))
            contents[str(getattr(content, "ID", ""))] = {
                "provider_id": provider_id,
                "title": str(getattr(content, "Title", "") or ""),
                "artist": self._related_name(content, "Artist", "Artist"),
                "album": self._related_name(content, "Album", "Album"),
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
        songs = sorted(self.db.get_playlist_songs(PlaylistID=target.ID), key=lambda item: int(getattr(item, "TrackNo", 0) or 0))
        for position, song in enumerate(songs, start=1):
            item = dict(contents.get(str(song.ContentID), {}))
            if item:
                item["position"] = position
                out.append(item)
        return out

    def playlist_exists(self, playlist_name: str) -> bool:
        return self._playlist_by_name(playlist_name) is not None

    def external_files_for_playlist(self, _playlist_name: str, desired: list[dict]) -> list[Path]:
        root = self.db_path.parent
        files = [root / "masterPlaylists6.xml"]
        contents = self._content_by_provider_id()
        for item in desired:
            content = contents.get(str(item["provider_id"]))
            if content is None:
                continue
            try:
                files.extend(Path(path) for path in self.db.read_anlz_files(content).keys())
            except Exception:
                raise
        return sorted(dict.fromkeys(files), key=lambda path: str(path))

    def begin(self) -> None:
        pass

    def apply_operations(self, plan: dict) -> None:
        playlist = self._playlist_by_name(plan.get("_playlist_name"))
        if playlist is None:
            if plan.get("_create_missing"):
                playlist = self._create_playlist(str(plan.get("_playlist_name") or "DeckPipe"))
            else:
                raise RuntimeError("playlist not found")
        contents = self._content_by_provider_id()
        desired_contents = []
        for desired in plan["desired_resolved"]:
            content = contents.get(desired["provider_id"])
            if content is None:
                content = self._content_by_path(desired["path"])
            if content is None:
                content = self._create_content(desired)
            self._apply_content_fields(content, desired)
            contents[desired["provider_id"]] = content
            desired_contents.append((desired, content))

        current_songs = list(self.db.get_playlist_songs(PlaylistID=playlist.ID))
        for song in sorted(current_songs, key=lambda item: int(getattr(item, "TrackNo", 0) or 0)):
            self.db.delete(song)
        for position, (_desired, content) in enumerate(desired_contents, start=1):
            self.db.add(self._create_playlist_song(playlist.ID, getattr(content, "ID"), position))

    def save_external_files(self) -> None:
        for path, anlz in sorted(self._staged_anlz.items(), key=lambda item: str(item[0])):
            anlz.save(path)
        self._staged_anlz.clear()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def close(self) -> None:
        self.db.close()

    def reopen(self):
        return _PyrekordboxAdapter()

    def _playlist_by_name(self, playlist_name: str | None):
        for playlist in self.db.get_playlist():
            if playlist.Attribute == 0 and (playlist_name is None or str(playlist.Name) == playlist_name):
                return playlist
        return None

    def _create_playlist(self, playlist_name: str):
        return self.db.create_playlist(playlist_name)

    def _content_provider_id(self, content) -> str:
        return str(getattr(content, "Commnt", "") or getattr(content, "Comments", "") or getattr(content, "ID", ""))

    def _content_by_provider_id(self) -> dict[str, object]:
        return {self._content_provider_id(content): content for content in self.db.get_content()}

    def _content_by_path(self, path: str):
        needle = str(path)
        for content in self.db.get_content():
            if str(getattr(content, "FolderPath", "") or "") == needle:
                return content
        return None

    def _one_or_none(self, query):
        if hasattr(query, "one_or_none"):
            return query.one_or_none()
        values = list(query)
        if len(values) > 1:
            raise RuntimeError("ambiguous rekordbox metadata row")
        return values[0] if values else None

    def _artist_by_name(self, name: str):
        return self._one_or_none(self.db.get_artist(Name=name))

    def _album_by_name(self, name: str):
        return self._one_or_none(self.db.get_album(Name=name))

    def _resolve_artist(self, name: str):
        artist = self._artist_by_name(name)
        if artist is None:
            artist = self.db.add_artist(name=name)
        return artist

    def _resolve_album(self, name: str, artist):
        album = self._album_by_name(name)
        if album is None:
            album = self.db.add_album(name=name, artist=artist)
        return album

    def _resolve_metadata_ids(self, artist_name: str, album_name: str) -> tuple[str, str]:
        artist = self._resolve_artist(artist_name)
        album = self._resolve_album(album_name, artist)
        return str(artist.ID), str(album.ID)

    def _file_type_value(self, path: Path) -> int | None:
        from pyrekordbox.db6.tables import FileType

        suffix = path.suffix.lstrip(".").upper()
        if not suffix:
            return None
        return getattr(FileType, suffix).value

    def _create_content(self, desired: dict):
        path = Path(desired["path"])
        artist_id, album_id = self._resolve_metadata_ids(str(desired["artist"]), str(desired["album"]))
        return self.db.add_content(
            path,
            Title=desired["title"],
            ArtistID=artist_id,
            AlbumID=album_id,
            Commnt=desired["provider_id"],
            Length=int(desired["duration"] or 0),
        )

    def _apply_content_fields(self, content, desired: dict) -> None:
        old_path = str(getattr(content, "FolderPath", "") or "")
        new_path = str(desired["path"])
        new_path_obj = Path(new_path)
        content.Title = desired["title"]
        content.Length = int(desired["duration"] or 0)
        if hasattr(content, "Commnt"):
            content.Commnt = desired["provider_id"]
        elif hasattr(content, "Comments"):
            content.Comments = desired["provider_id"]
        artist_id, album_id = self._resolve_metadata_ids(str(desired["artist"]), str(desired["album"]))
        if hasattr(content, "ArtistID"):
            content.ArtistID = artist_id
        if hasattr(content, "AlbumID"):
            content.AlbumID = album_id
        if old_path != new_path:
            self._stage_anlz_path_updates(content, new_path)
            content.FolderPath = new_path
            if str(getattr(content, "OrgFolderPath", "") or "") == old_path:
                content.OrgFolderPath = new_path
            if hasattr(content, "FileNameL"):
                content.FileNameL = new_path_obj.name
            if hasattr(content, "FileNameS"):
                content.FileNameS = new_path_obj.name
            if hasattr(content, "FileSize"):
                content.FileSize = new_path_obj.stat().st_size
            if hasattr(content, "FileType"):
                content.FileType = self._file_type_value(new_path_obj)

    def _stage_anlz_path_updates(self, content, new_path: str) -> None:
        rb_path = new_path.replace("\\", "/")
        for anlz_path, anlz in self.db.read_anlz_files(content).items():
            anlz.set_path(rb_path)
            self._staged_anlz[Path(anlz_path)] = anlz

    def _create_playlist_song(self, playlist_id: str, content_id: str, position: int):
        from pyrekordbox.db6 import tables

        now = datetime.now()
        return tables.DjmdSongPlaylist.create(
            ID=str(uuid.uuid4()),
            PlaylistID=str(playlist_id),
            ContentID=str(content_id),
            TrackNo=int(position),
            UUID=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
        )
