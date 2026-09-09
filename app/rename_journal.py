from __future__ import annotations

import os
import uuid
from pathlib import Path

from .atomic_io import AtomicIOError, atomic_load_json, atomic_write_json


JOURNAL_NAME = ".deckpipe-renames.json"
JOURNAL_VERSION = 1


def journal_path(playlist_dir: Path) -> Path:
    return Path(playlist_dir) / JOURNAL_NAME


def temporary_name(destination: str) -> str:
    suffix = Path(destination).suffix
    return f".deckpipe-rename-{uuid.uuid4().hex}.part{suffix}"


def _file_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid rename journal")
    path = Path(value)
    if path.is_absolute() or path.name != value or value in {".", ".."}:
        raise ValueError("invalid rename journal")
    return value


def validate_journal(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
        raise ValueError("invalid rename journal")
    operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("invalid rename journal")
    clean_operations = []
    keys = set()
    sources = set()
    temporaries = set()
    destinations = set()
    for raw in operations:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"]:
            raise ValueError("invalid rename journal")
        operation = {
            "key": raw["key"],
            "source": _file_name(raw.get("source")),
            "temporary": _file_name(raw.get("temporary")),
            "destination": _file_name(raw.get("destination")),
            "position": raw.get("position"),
        }
        if not isinstance(operation["position"], int) or isinstance(operation["position"], bool) or operation["position"] < 1:
            raise ValueError("invalid rename journal")
        if operation["source"] == operation["destination"]:
            raise ValueError("invalid rename journal")
        if operation["key"] in keys or operation["source"] in sources or operation["temporary"] in temporaries or operation["destination"] in destinations:
            raise ValueError("invalid rename journal")
        keys.add(operation["key"])
        sources.add(operation["source"])
        temporaries.add(operation["temporary"])
        destinations.add(operation["destination"])
        clean_operations.append(operation)
    if sources & temporaries or destinations & temporaries:
        raise ValueError("invalid rename journal")
    return {"version": JOURNAL_VERSION, "operations": clean_operations}


def save(playlist_dir: Path, operations: list[dict]) -> None:
    atomic_write_json(
        journal_path(playlist_dir),
        {"version": JOURNAL_VERSION, "operations": operations},
        validator=validate_journal,
        backup=False,
    )


def load(playlist_dir: Path) -> dict | None:
    path = journal_path(playlist_dir)
    if not path.exists():
        return None
    return atomic_load_json(path, default=None, validator=validate_journal, backup=False)


def move_new(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    if source.parent != destination.parent:
        raise AtomicIOError("rename journal path is invalid")
    if destination.exists():
        raise AtomicIOError("rename destination already exists")
    try:
        os.rename(source, destination)
    except Exception:
        raise AtomicIOError("playlist rename failed") from None


def remove(playlist_dir: Path) -> None:
    try:
        journal_path(playlist_dir).unlink()
    except FileNotFoundError:
        return
    except Exception:
        raise AtomicIOError("rename journal cleanup failed") from None
