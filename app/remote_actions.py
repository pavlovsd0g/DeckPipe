from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path

from .atomic_io import atomic_load_json, atomic_write_json, file_lock


REMOTE_ACTIONS_VERSION = 1
_VALID_STATES = {"pending", "failed", "succeeded"}


def _safe_string(value: object, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= 512 and (allow_empty or bool(value))


def _validate_error(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid remote actions")
    if set(value) != {"code", "service", "message", "retryable"}:
        raise ValueError("invalid remote actions")
    if not all(_safe_string(value.get(field)) for field in ("code", "service", "message")):
        raise ValueError("invalid remote actions")
    if not isinstance(value.get("retryable"), bool):
        raise ValueError("invalid remote actions")
    return dict(value)


def _validate_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("invalid remote actions")
    clean: list[str] = []
    seen: set[str] = set()
    for item in value:
        item = str(item)
        if not _safe_string(item) or item in seen:
            if item in seen:
                continue
            raise ValueError("invalid remote actions")
        seen.add(item)
        clean.append(item)
    return clean


def validate_remote_actions(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("version") != REMOTE_ACTIONS_VERSION:
        raise ValueError("invalid remote actions")
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        raise ValueError("invalid remote actions")
    clean = {"version": REMOTE_ACTIONS_VERSION, "actions": {}}
    for action_id, raw in actions.items():
        if not _safe_string(action_id) or not isinstance(raw, dict) or raw.get("id") != action_id:
            raise ValueError("invalid remote actions")
        if raw.get("service") != "deezer" or raw.get("operation") != "add_tracks_to_playlist":
            raise ValueError("invalid remote actions")
        if raw.get("state") not in _VALID_STATES or not _safe_string(raw.get("target_id")):
            raise ValueError("invalid remote actions")
        if not isinstance(raw.get("attempts"), int) or raw["attempts"] < 0:
            raise ValueError("invalid remote actions")
        if not isinstance(raw.get("created_at"), (int, float)) or not isinstance(raw.get("updated_at"), (int, float)):
            raise ValueError("invalid remote actions")
        action = {
            "id": action_id,
            "service": "deezer",
            "operation": "add_tracks_to_playlist",
            "target_id": raw["target_id"],
            "track_ids": _validate_ids(raw.get("track_ids")),
            "state": raw["state"],
            "attempts": raw["attempts"],
            "created_at": raw["created_at"],
            "updated_at": raw["updated_at"],
            "last_error": _validate_error(raw.get("last_error")),
            "added_track_ids": _validate_ids(raw.get("added_track_ids", [])),
            "already_present_track_ids": _validate_ids(raw.get("already_present_track_ids", [])),
        }
        clean["actions"][action_id] = action
    return clean


class RemoteActionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load_unlocked(self) -> dict:
        return atomic_load_json(
            self.path,
            default={"version": REMOTE_ACTIONS_VERSION, "actions": {}},
            validator=validate_remote_actions,
            backup=True,
        )

    def _write_unlocked(self, payload: dict) -> None:
        atomic_write_json(self.path, payload, validator=validate_remote_actions, backup=True)

    def _mutate(self, action_id: str, update) -> dict:
        with file_lock(self.path):
            payload = self._load_unlocked()
            action = payload["actions"].get(action_id)
            if action is None:
                raise KeyError(action_id)
            update(action)
            action["updated_at"] = time.time()
            self._write_unlocked(payload)
            return copy.deepcopy(action)

    def create_deezer_add(self, target_id: str, track_ids: list[str]) -> dict:
        clean_ids = _validate_ids([str(item) for item in track_ids])
        if not clean_ids or not _safe_string(str(target_id)):
            raise ValueError("invalid remote action")
        now = time.time()
        action_id = uuid.uuid4().hex[:12]
        action = {
            "id": action_id,
            "service": "deezer",
            "operation": "add_tracks_to_playlist",
            "target_id": str(target_id),
            "track_ids": clean_ids,
            "state": "pending",
            "attempts": 1,
            "created_at": now,
            "updated_at": now,
            "last_error": None,
            "added_track_ids": [],
            "already_present_track_ids": [],
        }
        with file_lock(self.path):
            payload = self._load_unlocked()
            payload["actions"][action_id] = action
            self._write_unlocked(payload)
        return copy.deepcopy(action)

    def get(self, action_id: str) -> dict | None:
        with file_lock(self.path):
            action = self._load_unlocked()["actions"].get(action_id)
            return copy.deepcopy(action) if action is not None else None

    def list(self) -> list[dict]:
        with file_lock(self.path):
            actions = list(self._load_unlocked()["actions"].values())
        return copy.deepcopy(sorted(actions, key=lambda item: item["created_at"], reverse=True))

    def mark_retrying(self, action_id: str) -> dict:
        def update(action: dict) -> None:
            action["state"] = "pending"
            action["attempts"] += 1
            action["last_error"] = None

        return self._mutate(action_id, update)

    def mark_failed(self, action_id: str, error: dict) -> dict:
        clean_error = _validate_error(error)

        def update(action: dict) -> None:
            action["state"] = "failed"
            action["last_error"] = clean_error

        return self._mutate(action_id, update)

    def mark_succeeded(
        self,
        action_id: str,
        *,
        added_track_ids: list[str],
        already_present_track_ids: list[str] | None = None,
    ) -> dict:
        added = _validate_ids([str(item) for item in added_track_ids])
        present = _validate_ids([str(item) for item in (already_present_track_ids or [])])

        def update(action: dict) -> None:
            action["state"] = "succeeded"
            action["last_error"] = None
            action["added_track_ids"] = added
            action["already_present_track_ids"] = present

        return self._mutate(action_id, update)
