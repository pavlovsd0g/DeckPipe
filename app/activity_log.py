"""Bounded, best-effort operational events in the explicit application profile.

Callers supply fixed human-readable messages, never exception dumps, provider
responses, configuration, paths, or credentials. Redaction is a second boundary.
One backend process owns a profile; its concurrent threads share the writer lock.
GET pages are newest first; ``before`` is an exclusive, restart-stable event ID.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 5
MAX_MESSAGE_CHARS = 1000
_CONTEXT_KEYS = frozenset({"job_id", "track_id", "playlist_id", "error_code"})
_ID = re.compile(r"^[0-9]{20}$")
_LABEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+")
_HEADER_SECRET = re.compile(r"(?im)\b(?:authorization|cookies?)\b[\"']?\s*(?:[:=]\s*|\s+)[^\r\n]+")
_DUMP = re.compile(r"(?i)(?:traceback\s*\(most recent call last\):|[\{\[]\s*[\"'])")
_SECRET = re.compile(
    r"(?i)\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?(?:key|token)|"
    r"token|secret|password|passwd|credentials?|arl)\b[\"']?\s*(?:[:=]\s*|\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(?:bearer|oauth|basic)\s+[^\s,;\"']+")
_QUERY_VALUE = re.compile(r"([?&]?[a-zA-Z_][a-zA-Z0-9_.%-]*=)(?:\"[^\"]*\"|'[^']*'|[^\s&#;,]+)")
_OPAQUE_SECRET = re.compile(r"(?<![a-zA-Z0-9_-])(?:eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|[a-zA-Z0-9_+/=-]{48,})(?![a-zA-Z0-9_-])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_lock = threading.RLock()
_directory: Path | None = None
_last_id = 0
_warned: set[str] = set()
_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs", tags=["operations"])


def _warn_once(code: str) -> None:
    """Even a broken application logging handler must not break an operation."""
    if code in _warned:
        return
    _warned.add(code)
    try:
        _logger.warning("DeckPipe activity log unavailable (%s)", code)
    except Exception:
        pass


def _safe_text(value: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    text = value[:8192]
    if _DUMP.search(text):
        return "[redacted-detail]"
    text = _URL.sub("[redacted-url]", text)
    text = _HEADER_SECRET.sub("[redacted]", text)
    text = _SECRET.sub("[redacted]", text)
    text = _AUTH_SCHEME.sub("[redacted]", text)
    text = _QUERY_VALUE.sub(lambda match: match.group(1) + "[redacted]", text)
    text = _OPAQUE_SECRET.sub("[redacted]", text)
    return _CONTROL.sub(" ", text).strip()[:limit]


def _label(value: object) -> str:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "unknown"


def _fields(level: object, operation: object, stage: object, message: object, context: dict) -> dict:
    normalized = level.lower() if isinstance(level, str) else "info"
    fields = {
        "level": normalized if normalized in {"debug", "info", "warning", "error"} else "info",
        "operation": _label(operation), "stage": _label(stage), "message": _safe_text(message),
    }
    for key in _CONTEXT_KEYS:
        value = context.get(key)
        if isinstance(value, str):
            fields[key] = _safe_text(value, 128)
        elif value is None or type(value) is bool:
            if key in context:
                fields[key] = value
        elif type(value) is int and value.bit_length() <= 64:
            fields[key] = value
        elif type(value) is float and math.isfinite(value):
            fields[key] = value
    return fields


def _path(index: int = 0) -> Path:
    assert _directory is not None
    return _directory / ("activity.jsonl" if index == 0 else f"activity.{index}.jsonl")


def _read_entries() -> list[dict]:
    if _directory is None:
        return []
    entries: dict[str, dict] = {}
    for index in range(MAX_FILES - 1, -1, -1):
        try:
            with _path(index).open("rb") as stream:
                # A corrupt or externally replaced file cannot cause an unbounded read.
                data = stream.read(MAX_FILE_BYTES)
        except FileNotFoundError:
            continue
        except OSError:
            _warn_once("read_failed")
            continue
        for line in data.splitlines():
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not _ID.fullmatch(entry["id"]):
                    continue
                timestamp = entry.get("time")
                if not isinstance(timestamp, str) or len(timestamp) > 40 or not timestamp.endswith("Z"):
                    continue
                datetime.fromisoformat(timestamp)
                if not all(key in entry for key in ("level", "operation", "stage", "message")):
                    continue
                entries[entry["id"]] = {"id": entry["id"], "time": timestamp, **_fields(entry["level"], entry["operation"], entry["stage"], entry["message"], entry)}
            except (ValueError, UnicodeError, TypeError, RecursionError):
                continue
    return sorted(entries.values(), key=lambda entry: entry["id"], reverse=True)


def _repair_tail() -> None:
    path = _path()
    if not path.exists() or not path.stat().st_size:
        return
    with path.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) == b"\n":
            return
        stream.seek(max(0, path.stat().st_size - MAX_FILE_BYTES))
        start = stream.tell()
        tail = stream.read(MAX_FILE_BYTES)
        newline = tail.rfind(b"\n")
        stream.truncate(start + newline + 1 if newline >= 0 else 0)
        stream.flush()
        os.fsync(stream.fileno())


def initialize(data_root: Path) -> None:
    """Select profile storage. Failure leaves all primary operations usable."""
    global _directory, _last_id
    with _lock:
        _directory = None
        _last_id = 0
        _warned.clear()
        try:
            _directory = Path(data_root) / "logs"
            _directory.mkdir(parents=True, exist_ok=True)
            _repair_tail()
            rows = _read_entries()
            if rows:
                _last_id = int(rows[0]["id"])
        except Exception:
            _warn_once("initialize_failed")


def _rotate() -> None:
    _path(MAX_FILES - 1).unlink(missing_ok=True)
    for index in range(MAX_FILES - 2, -1, -1):
        source = _path(index)
        if source.exists():
            source.replace(_path(index + 1))


def record(level: str, operation: str, stage: str, message: str, **context) -> None:
    """Append and fsync a safe event. Never propagate a logging failure."""
    global _last_id
    try:
        with _lock:
            if _directory is None:
                _warn_once("not_initialized")
                return
            _directory.mkdir(parents=True, exist_ok=True)
            next_id = max(time.time_ns(), _last_id + 1)
            # A failed flush may still have appended a complete row. Reserve the
            # identifier before any IO so a retry cannot reuse that row's ID.
            _last_id = next_id
            entry = {
                "id": f"{next_id:020d}",
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                **_fields(level, operation, stage, message, context),
            }
            encoded = (json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES:
                _warn_once("entry_too_large")
                return
            _repair_tail()
            path = _path()
            size = path.stat().st_size if path.exists() else 0
            if size + len(encoded) > MAX_FILE_BYTES:
                _rotate()
            with path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        _warn_once("write_failed")


def list_entries(*, limit: int = 200, before: str | None = None) -> dict:
    """Read newest-first retained events, optionally older than an event ID."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if before is not None and (not isinstance(before, str) or not _ID.fullmatch(before)):
        raise ValueError("invalid log cursor")
    with _lock:
        rows = _read_entries()
        if before is not None:
            rows = [row for row in rows if row["id"] < before]
        page = rows[:limit]
        more = len(rows) > limit
        return {"entries": page, "next_before": page[-1]["id"] if more else None, "has_more": more}


@router.get("")
def api_logs(
    limit: int = Query(default=200, ge=1, le=500),
    before: str | None = Query(default=None, pattern=r"^[0-9]{20}$"),
    cursor: str | None = Query(default=None, pattern=r"^[0-9]{20}$"),
) -> dict:
    if before is not None and cursor is not None and before != cursor:
        raise HTTPException(400, "Conflicting log cursors")
    return list_entries(limit=limit, before=before or cursor)


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["rekordbox_sync_cancelled", "rekordbox_sync_succeeded", "rekordbox_sync_failed", "rekordbox_sync_unchanged"]
    message: str | None = Field(default=None, max_length=500)


_CLIENT_EVENTS = {
    "rekordbox_sync_cancelled": ("warning", "sync_cancelled", "Rekordbox sync cancelled before confirmation."),
    "rekordbox_sync_succeeded": ("info", "sync_succeeded", "Rekordbox sync completed."),
    "rekordbox_sync_failed": ("error", "sync_failed", "Rekordbox sync failed."),
    "rekordbox_sync_unchanged": ("info", "sync_unchanged", "Rekordbox playlist already contains the selected tracks."),
}


@router.post("/events")
def api_client_event(event: ClientEvent) -> dict:
    level, stage, message = _CLIENT_EVENTS[event.event]
    record(level, "rekordbox", stage, event.message or message)
    return {"accepted": True}


def _http_operation(method: str, path: str) -> tuple[str, str] | None:
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    for prefix, operation, label in (
        ("/api/library/", "library", "Music library operation"),
        ("/api/rb/", "rekordbox", "Rekordbox operation"),
        ("/api/jobs/", "jobs", "Download queue operation"),
    ):
        if path.startswith(prefix):
            return operation, label
    if path in {"/api/search/download", "/api/flip", "/api/errors/retry"} or re.fullmatch(r"/api/(?:playlists|sc/sources)/[^/]+/download", path):
        return "download", "Download operation"
    return None


class ActivityLogMiddleware:
    """Log mutation boundaries without inspecting headers, queries or bodies.

    Place inside LoopbackSecurityMiddleware so rejected/authentication requests
    are not recorded. Polling and the logs endpoints are deliberately excluded.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        selected = _http_operation(scope.get("method", ""), scope.get("path", "")) if scope.get("type") == "http" else None
        if selected is None:
            await self.app(scope, receive, send)
            return
        operation, label = selected
        record("info", operation, "request_started", label + " requested.")
        status = None

        async def observed_send(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            record("warning", operation, "request_failed", label + " request cancelled.", error_code="request_cancelled")
            raise
        except Exception:
            record("error", operation, "request_failed", label + " failed.", error_code="request_exception")
            raise
        if status is None or status >= 400:
            level = "error" if status is None or status >= 500 else "warning"
            record(level, operation, "request_failed", label + " failed.", error_code=f"http_{status}" if status is not None else "response_incomplete")
        else:
            record("info", operation, "request_finished", label + " request completed.")
