from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


STAGING_MARKER = ".deckpipe-stage-"
PARTIAL_MARKER = ".part"
JsonValidator = Callable[[object], object]
replace_file = os.replace

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_HELD = threading.local()


class AtomicIOError(RuntimeError):
    """Generic non-secret durable I/O failure."""


def _canonical(path: Path) -> str:
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(Path(path).absolute())


def lock_path_for(target: Path) -> Path:
    target = Path(target)
    if target.exists() and target.is_dir():
        return target / ".deckpipe.lock"
    return target.with_name(f"{target.name}.lock")


def _thread_lock(lock_path: Path) -> threading.RLock:
    key = _canonical(lock_path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


if os.name == "nt":
    import msvcrt

    def _lock_file(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_file(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(target: Path) -> Iterator[None]:
    lock_path = lock_path_for(Path(target))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(lock_path)
    key = _canonical(lock_path)
    held = getattr(_HELD, "paths", set())
    if key in held:
        with thread_lock:
            yield
        return
    with thread_lock:
        with lock_path.open("a+b") as handle:
            try:
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                _lock_file(handle)
                held = set(getattr(_HELD, "paths", set()))
                held.add(key)
                _HELD.paths = held
                yield
            finally:
                held = set(getattr(_HELD, "paths", set()))
                held.discard(key)
                _HELD.paths = held
                _unlock_file(handle)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except Exception:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_payload(path: Path, payload: object, *, replace: Callable[[Path, Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}{STAGING_MARKER}{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace(tmp, path)
        _fsync_parent(path)
    except Exception:
        raise AtomicIOError("atomic JSON write failed") from None
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _replace_json_payload(path: Path, payload: object, *, replace: Callable[[Path, Path], None]) -> None:
    _write_json_payload(path, payload, replace=replace)


def _remove_path(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        raise AtomicIOError("atomic JSON write failed") from None


def _read_json(path: Path, validator: JsonValidator | None) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return validator(payload) if validator else payload
    except Exception:
        raise AtomicIOError("JSON state is unreadable") from None


def atomic_write_json(
    path: Path,
    payload: object,
    *,
    backup: bool = False,
    validator: JsonValidator | None = None,
    replace: Callable[[Path, Path], None] | None = None,
) -> None:
    path = Path(path)
    replace = replace or replace_file
    candidate = validator(copy.deepcopy(payload)) if validator else payload
    with file_lock(path):
        if not backup:
            _replace_json_payload(path, candidate, replace=replace)
            return

        previous = None
        bak = backup_path(path)
        if path.exists():
            try:
                previous = _read_json(path, validator)
            except AtomicIOError:
                if bak.exists():
                    previous = _read_json(bak, validator)

        if previous is None:
            try:
                _replace_json_payload(bak, candidate, replace=replace)
                _replace_json_payload(path, candidate, replace=replace)
            except Exception:
                _remove_path(path)
                _remove_path(bak)
                raise AtomicIOError("atomic JSON write failed") from None
            return

        _replace_json_payload(bak, previous, replace=replace)
        try:
            _replace_json_payload(path, candidate, replace=replace)
        except Exception:
            try:
                _replace_json_payload(path, previous, replace=replace)
            except Exception:
                pass
            raise AtomicIOError("atomic JSON write failed") from None


def backup_path(path: Path) -> Path:
    return Path(path).with_name(f"{Path(path).name}.bak")


def atomic_load_json(
    path: Path,
    *,
    default: object,
    validator: JsonValidator | None = None,
    backup: bool = False,
    return_recovered: bool = False,
) -> object | tuple[object, bool]:
    path = Path(path)
    with file_lock(path):
        if path.exists():
            try:
                payload = _read_json(path, validator)
                return (payload, False) if return_recovered else payload
            except AtomicIOError:
                if not backup:
                    raise
        if backup:
            bak = backup_path(path)
            if bak.exists():
                try:
                    payload = _read_json(bak, validator)
                    return (payload, True) if return_recovered else payload
                except AtomicIOError:
                    pass
        fallback = copy.deepcopy(default)
        return (fallback, False) if return_recovered else fallback


def make_staged_path(final_path: Path) -> Path:
    final_path = Path(final_path)
    return final_path.with_name(f"{final_path.stem}{STAGING_MARKER}{uuid.uuid4().hex}.part{final_path.suffix}")


def is_partial_path(path_or_name: Path | str) -> bool:
    name = Path(path_or_name).name.lower()
    return STAGING_MARKER in name or name.endswith(PARTIAL_MARKER) or f"{PARTIAL_MARKER}." in name


def final_path_from_stage(stage_path: Path) -> Path:
    path = Path(stage_path)
    name = path.name
    marker = name.lower().rfind(STAGING_MARKER)
    if marker < 0:
        return path
    return path.with_name(name[:marker] + path.suffix)


def fsync_file(path: Path) -> None:
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def publish_staged_file(stage_path: Path, final_path: Path) -> Path:
    stage_path = Path(stage_path)
    final_path = Path(final_path)
    if stage_path.parent != final_path.parent or not is_partial_path(stage_path):
        raise AtomicIOError("invalid staged publication")
    with file_lock(final_path):
        try:
            fsync_file(stage_path)
            replace_file(stage_path, final_path)
            _fsync_parent(final_path)
        except Exception:
            raise AtomicIOError("staged publication failed") from None
        finally:
            try:
                stage_path.unlink()
            except FileNotFoundError:
                pass
    return final_path


def cleanup_owned_stages(*paths: Path | None) -> None:
    for path in paths:
        if path is None:
            continue
        candidate = Path(path)
        if not is_partial_path(candidate):
            continue
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
