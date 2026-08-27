# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .atomic_io import (
    atomic_load_json,
    atomic_write_json,
    cleanup_owned_stages,
    final_path_from_stage,
    is_partial_path,
    publish_staged_file,
)
from .deezer_client import get_session, load_config, verify_file
from .library import is_ready_entry, load_sidecar, max_position, numbered_name, playlist_dir, save_sidecar, track_key, update_track_status

AUTO_RETRIES = 2
JOURNAL_VERSION = 1
IDEMPOTENT_DOWNLOAD_MODES = {"append", "playlist_order"}

_jobs: dict[str, dict] = {}
_queue: list[str] = []
_lock = threading.RLock()
_worker_started = False
_initialized = False
_journal_path: Path | None = None
_start_worker_default = True
_write_json = atomic_write_json
_real_atomic_write_json = atomic_write_json
_worker_thread: threading.Thread | None = None
_worker_generation = 0
_worker_stop = threading.Event()

_VALID_STATES = {"queued", "running", "done"}
_VALID_OUTCOMES = {"pending", "succeeded", "partial_failure", "failed", "interrupted"}
_TERMINAL_OUTCOMES = {"succeeded", "partial_failure", "failed", "interrupted"}
_VALID_MODES = {"append", "playlist_order", "flip_to_wav", "flip_to_source"}
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIRTY_ERROR_RE = re.compile(
    r"(\.deckpipe-stage-|\.part\.|generated-[A-Za-z0-9_-]*|signed-url|[A-Za-z]:[\\/]|https?://|\b(token|secret|credential|arl)\b)",
    re.IGNORECASE,
)


class _StaleWorker(RuntimeError):
    pass


def _now():
    return int(time.time())


def _validate_journal(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid journal")
    if payload.get("version") != JOURNAL_VERSION:
        raise ValueError("invalid journal")
    jobs = payload.get("jobs", {})
    if not isinstance(jobs, dict):
        raise ValueError("invalid journal")
    clean = {"version": JOURNAL_VERSION, "jobs": {}}
    for job_id, job in jobs.items():
        if not isinstance(job_id, str) or not isinstance(job, dict):
            raise ValueError("invalid journal")
        clean_job = dict(job)
        if clean_job.get("id") != job_id:
            raise ValueError("invalid journal")
        if not _safe_string(clean_job.get("playlist_id")) or not _safe_string(clean_job.get("title")):
            raise ValueError("invalid journal")
        if clean_job.get("mode") not in _VALID_MODES:
            raise ValueError("invalid journal")
        if clean_job.get("state") not in _VALID_STATES or clean_job.get("outcome") not in _VALID_OUTCOMES:
            raise ValueError("invalid journal")
        if clean_job["state"] == "done":
            if clean_job["outcome"] not in _TERMINAL_OUTCOMES:
                raise ValueError("invalid journal")
        elif clean_job["outcome"] != "pending":
            raise ValueError("invalid journal")
        if not _finite_number(clean_job.get("created_at")):
            raise ValueError("invalid journal")
        for field in ("total", "done", "failed"):
            if not _nonnegative_int(clean_job.get(field)):
                raise ValueError("invalid journal")
        if clean_job["failed"] > clean_job["done"] or clean_job["done"] > clean_job["total"]:
            raise ValueError("invalid journal")
        if clean_job.get("current") is not None and not isinstance(clean_job.get("current"), str):
            raise ValueError("invalid journal")
        tracks = clean_job.get("tracks")
        results = clean_job.get("results")
        if not isinstance(tracks, list) or not isinstance(results, list):
            raise ValueError("invalid journal")
        clean_job["tracks"] = [_validate_track_item(item) for item in tracks]
        clean_job["results"] = [_validate_result_item(item) for item in results]
        _validate_job_coherence(clean_job)
        if clean_job["state"] == "done" and clean_job["outcome"] != "succeeded":
            clean_job["terminal_error"] = _validate_terminal_error(clean_job.get("terminal_error"))
        elif clean_job.get("terminal_error") is not None:
            if clean_job["state"] != "done":
                raise ValueError("invalid journal")
            clean_job["terminal_error"] = _validate_terminal_error(clean_job.get("terminal_error"))
        else:
            clean_job["terminal_error"] = None
        clean["jobs"][job_id] = clean_job
    return clean


def initialize(data_root: Path | None = None, *, start_worker: bool = True, write_json=None) -> None:
    global _initialized, _journal_path, _start_worker_default, _write_json, _jobs, _queue
    global _worker_started, _worker_generation, _worker_stop, _worker_thread
    if data_root is None:
        raise RuntimeError("DeckPipe jobs require an explicit data root")
    journal_path = Path(data_root) / "jobs.json"
    with _lock:
        if (
            _initialized
            and _journal_path == journal_path
            and _worker_thread is not None
            and _worker_thread.is_alive()
            and (_queue or any(job.get("state") == "running" for job in _jobs.values()))
        ):
            raise RuntimeError("DeckPipe jobs for this data root are busy")
        _worker_stop.set()
        _worker_generation += 1
        _worker_stop = threading.Event()
        _worker_thread = None
        _worker_started = False
        writer = write_json or atomic_write_json
        _worker_started = False
        payload, recovered = atomic_load_json(
            journal_path,
            default={"version": JOURNAL_VERSION, "jobs": {}},
            validator=_validate_journal,
            backup=True,
            return_recovered=True,
        )
        loaded_jobs = {job_id: _normalize_job(job) for job_id, job in payload["jobs"].items()}
        loaded_queue = []
        changed = False
        for job_id, job in loaded_jobs.items():
            if job.get("state") in ("queued", "running"):
                if job.get("mode") in IDEMPOTENT_DOWNLOAD_MODES:
                    if _reconcile_ready_items_in_jobs(loaded_jobs, job_id):
                        changed = True
                    if not _pending_track_ids_from_job(job):
                        job["state"] = "done"
                        job["outcome"] = "succeeded" if job.get("failed", 0) == 0 else "partial_failure"
                        job["current"] = None
                        changed = True
                        continue
                    job["state"] = "queued"
                    job["outcome"] = "pending"
                    job["current"] = None
                    if _pending_track_ids_from_job(job):
                        loaded_queue.append(job_id)
                    changed = True
                else:
                    job["state"] = "done"
                    job["outcome"] = "interrupted"
                    job["current"] = None
                    job["terminal_error"] = {
                        "code": "restart_interrupted_non_idempotent",
                        "message": "Job interrupted by restart and was not resumed",
                    }
                    changed = True
        if changed:
            writer(journal_path, {"version": JOURNAL_VERSION, "jobs": loaded_jobs}, validator=_validate_journal, backup=not recovered)
        _journal_path = journal_path
        _start_worker_default = start_worker
        _write_json = writer
        _jobs = loaded_jobs
        _queue = loaded_queue
        _initialized = True
        if start_worker and _queue:
            _ensure_worker_locked()


def _safe_string(value: object, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and (allow_empty or value != "")


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_provider(provider: object) -> bool:
    return isinstance(provider, str) and bool(_SAFE_PROVIDER_RE.match(provider))


def _validate_optional_track_number(track: dict, field: str) -> None:
    if field not in track:
        return
    value = track.get(field)
    if not _finite_number(value):
        raise ValueError("invalid journal")
    if field in {"position", "total"} and not _nonnegative_int(value):
        raise ValueError("invalid journal")


def _validate_track_item(item: object) -> dict:
    if not isinstance(item, dict):
        raise ValueError("invalid journal")
    raw = dict(item)
    if not _safe_string(str(raw.get("id")) if raw.get("id") is not None else None):
        raise ValueError("invalid journal")
    if not _safe_string(raw.get("title"), allow_empty=True):
        raise ValueError("invalid journal")
    provider = raw.get("provider", "deezer")
    if not _valid_provider(provider):
        raise ValueError("invalid journal")
    for field in ("artist", "album", "url"):
        if field in raw and not isinstance(raw.get(field), str):
            raise ValueError("invalid journal")
    for field in ("duration", "position", "total"):
        _validate_optional_track_number(raw, field)
    clean = {
        "provider": provider,
        "id": str(raw["id"]),
        "title": raw["title"],
    }
    for field in ("artist", "album", "url"):
        if field in raw:
            clean[field] = raw[field]
    for field in ("duration", "position", "total"):
        if field in raw:
            clean[field] = raw[field]
    return clean


def _canonicalize_tracks(tracks: list) -> list[dict]:
    clean_tracks = [_validate_track_item(item) for item in tracks]
    seen: set[str] = set()
    unique: list[dict] = []
    for track in clean_tracks:
        key = _result_key(track)
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)
    return unique


def _validate_result_item(item: object) -> dict:
    if not isinstance(item, dict):
        raise ValueError("invalid journal")
    raw = dict(item)
    if not _safe_string(str(raw.get("id")) if raw.get("id") is not None else None):
        raise ValueError("invalid journal")
    provider = raw.get("provider", "deezer")
    if not _valid_provider(provider):
        raise ValueError("invalid journal")
    track_id = str(raw["id"])
    key = str(raw.get("key") or f"{provider}:{track_id}")
    if key != f"{provider}:{track_id}":
        raise ValueError("invalid journal")
    if not isinstance(raw.get("ok"), bool):
        raise ValueError("invalid journal")
    for field in ("title", "error", "quality"):
        if not isinstance(raw.get(field, ""), str):
            raise ValueError("invalid journal")
    if "reconciled" in raw and not isinstance(raw["reconciled"], bool):
        raise ValueError("invalid journal")
    result = {
        "provider": provider,
        "id": track_id,
        "key": key,
        "title": raw.get("title", ""),
        "ok": raw["ok"],
        "error": _sanitize_public_text(raw.get("error", ""), fallback="operation failed"),
        "quality": raw.get("quality", ""),
    }
    if "reconciled" in raw:
        result["reconciled"] = raw["reconciled"]
    return result


def _validate_job_coherence(job: dict) -> None:
    track_keys = [_result_key(track) for track in job["tracks"]]
    result_keys = [str(item["key"]) for item in job["results"]]
    if len(set(track_keys)) != len(track_keys) or len(set(result_keys)) != len(result_keys):
        raise ValueError("invalid journal")
    if job.get("mode") in IDEMPOTENT_DOWNLOAD_MODES:
        if job["total"] != len(track_keys):
            raise ValueError("invalid journal")
        if any(key not in set(track_keys) for key in result_keys):
            raise ValueError("invalid journal")
    if job["done"] != len(result_keys):
        raise ValueError("invalid journal")
    failed_results = sum(1 for item in job["results"] if not item["ok"])
    if job["failed"] != failed_results:
        raise ValueError("invalid journal")
    if job["done"] > job["total"] or job["failed"] > job["done"]:
        raise ValueError("invalid journal")
    if job["state"] == "done":
        if job["outcome"] == "succeeded" and (job["failed"] != 0 or job["done"] != job["total"]):
            raise ValueError("invalid journal")
        if job["outcome"] == "partial_failure" and not (0 < job["failed"] < job["total"] and job["done"] == job["total"]):
            raise ValueError("invalid journal")


def _validate_terminal_error(error: object) -> dict:
    if not isinstance(error, dict):
        raise ValueError("invalid journal")
    code = error.get("code")
    message = error.get("message")
    if not _safe_string(code) or not _safe_string(message):
        raise ValueError("invalid journal")
    return {"code": code, "message": _sanitize_public_text(message, fallback="Job failed")}


def _normalize_job(job: dict) -> dict:
    normalized = dict(job)
    normalized.setdefault("state", "queued")
    normalized.setdefault("outcome", "pending" if normalized["state"] != "done" else "succeeded")
    normalized.setdefault("results", [])
    normalized.setdefault("tracks", [])
    normalized.setdefault("terminal_error", None)
    normalized.setdefault("done", len(normalized.get("results", [])))
    normalized.setdefault("failed", sum(1 for item in normalized.get("results", []) if not item.get("ok")))
    normalized.setdefault("current", None)
    return normalized


def _result_key(track: dict) -> str:
    return f"{track.get('provider', 'deezer')}:{track.get('id')}"


def _sanitize_public_text(value: object, *, fallback: str) -> str:
    text = value if isinstance(value, str) else ""
    if not text:
        return ""
    if _DIRTY_ERROR_RE.search(text):
        return fallback
    return text


def _sanitize_terminal_error(error: dict | None, *, outcome: str) -> dict | None:
    if outcome == "succeeded":
        return None
    if not isinstance(error, dict):
        return {"code": outcome, "message": "Job failed"}
    code = str(error.get("code") or outcome)
    if not _SAFE_PROVIDER_RE.match(code):
        code = outcome
    message = _sanitize_public_text(error.get("message", ""), fallback="Job failed") or "Job failed"
    return {"code": code, "message": message}


def _require_initialized() -> None:
    if not _initialized or _journal_path is None:
        raise RuntimeError("DeckPipe jobs are not initialized")


def _snapshot(job: dict | None):
    return copy.deepcopy(job) if job is not None else None


def _persist_locked(*, backup: bool = True) -> None:
    _require_initialized()
    payload = {"version": JOURNAL_VERSION, "jobs": _jobs}
    _write_json(_journal_path, payload, validator=_validate_journal, backup=backup)


def _write_jobs_candidate_locked(candidate_jobs: dict[str, dict], *, backup: bool = True) -> None:
    _require_initialized()
    _write_json(_journal_path, {"version": JOURNAL_VERSION, "jobs": candidate_jobs}, validator=_validate_journal, backup=backup)


def _publish_jobs_locked(candidate_jobs: dict[str, dict], candidate_queue: list[str] | None = None) -> None:
    global _jobs, _queue
    _jobs = candidate_jobs
    if candidate_queue is not None:
        _queue = candidate_queue


def _check_generation_locked(generation: int | None) -> None:
    if generation is not None and generation != _worker_generation:
        raise _StaleWorker()


def _ensure_worker_locked() -> None:
    global _worker_started, _worker_thread
    if not _worker_started or _worker_thread is None or not _worker_thread.is_alive():
        _worker_started = True
        _worker_thread = threading.Thread(target=_worker, args=(_worker_generation, _worker_stop), daemon=True)
        _worker_thread.start()


def _queue_job_locked(job_id: str, *, start_worker: bool | None) -> None:
    if job_id not in _queue:
        _queue.append(job_id)
    should_start = _start_worker_default if start_worker is None else start_worker
    if should_start:
        _ensure_worker_locked()


def enqueue(
    playlist_id: str,
    playlist_title: str,
    tracks: list,
    mode: str = "append",
    *,
    start_worker: bool | None = None,
) -> str:
    _require_initialized()
    job_id = uuid.uuid4().hex[:8]
    canonical_tracks = _canonicalize_tracks(tracks)
    job = {
        "id": job_id,
        "playlist_id": playlist_id,
        "title": playlist_title,
        "total": len(canonical_tracks),
        "done": 0,
        "failed": 0,
        "current": None,
        "state": "queued",
        "outcome": "pending",
        "mode": mode,
        "results": [],
        "tracks": canonical_tracks,
        "created_at": _now(),
        "terminal_error": None,
    }
    with _lock:
        candidate_jobs = copy.deepcopy(_jobs)
        candidate_queue = list(_queue)
        candidate_jobs[job_id] = job
        if job_id not in candidate_queue:
            candidate_queue.append(job_id)
        should_start = _start_worker_default if start_worker is None else start_worker
        _write_jobs_candidate_locked(candidate_jobs)
        _publish_jobs_locked(candidate_jobs, candidate_queue)
        if should_start:
            _ensure_worker_locked()
    return job_id


def get_job(job_id: str):
    with _lock:
        return _snapshot(_jobs.get(job_id))


def list_jobs():
    with _lock:
        return copy.deepcopy(sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)[:20])


def pending_track_ids(job_id: str) -> list[dict]:
    job = _jobs[job_id]
    return _pending_track_ids_from_job(job)


def _pending_track_ids_from_job(job: dict) -> list[dict]:
    completed = {str(item.get("key") or f"{item.get('provider', 'deezer')}:{item.get('id')}") for item in job.get("results", [])}
    return [dict(t) for t in job.get("tracks", []) if _result_key(t) not in completed]


def _reconcile_ready_items_locked(job_id: str) -> bool:
    return _reconcile_ready_items_in_jobs(_jobs, job_id)


def _reconcile_ready_items_in_jobs(jobs_map: dict[str, dict], job_id: str) -> bool:
    job = jobs_map[job_id]
    if job.get("mode") not in IDEMPOTENT_DOWNLOAD_MODES:
        return False
    changed = False
    try:
        pl_dir = playlist_dir(job["playlist_id"], job["title"])
        sidecar = load_sidecar(pl_dir)
    except Exception:
        return False
    results_by_key = {str(item.get("key") or f"{item.get('provider', 'deezer')}:{item.get('id')}") for item in job["results"]}
    for track in job.get("tracks", []):
        key = _result_key(track)
        if key in results_by_key:
            continue
        provider = track.get("provider", "deezer")
        entry = sidecar.get("tracks", {}).get(track_key(track.get("id"), provider))
        if entry is not None and is_ready_entry(pl_dir, entry):
            job["results"].append(
                {
                    "id": str(track.get("id")),
                    "provider": provider,
                    "key": key,
                    "title": track.get("title", ""),
                    "ok": True,
                    "error": "",
                    "quality": "reconciled",
                    "reconciled": True,
                }
            )
            job["done"] += 1
            results_by_key.add(key)
            changed = True
    return changed


def mark_running(job_id: str, *, _generation: int | None = None) -> None:
    with _lock:
        _check_generation_locked(_generation)
        job = _jobs[job_id]
        if job["state"] == "done":
            raise RuntimeError("terminal job cannot run again")
        if job["state"] == "queued":
            candidate_jobs = copy.deepcopy(_jobs)
            candidate_jobs[job_id]["state"] = "running"
            candidate_jobs[job_id]["outcome"] = "pending"
            _write_jobs_candidate_locked(candidate_jobs)
            _publish_jobs_locked(candidate_jobs)


def mark_item_complete(job_id: str, track: dict, *, ok: bool, error: str, quality: str, _generation: int | None = None) -> None:
    with _lock:
        _check_generation_locked(_generation)
        job = _jobs[job_id]
        if job["state"] != "running":
            raise RuntimeError("terminal job cannot accept progress")
        track_id = str(track["id"])
        provider = track.get("provider", "deezer")
        key = _result_key(track)
        if any(str(item.get("key") or f"{item.get('provider', 'deezer')}:{item.get('id')}") == key for item in job["results"]):
            return
        candidate_jobs = copy.deepcopy(_jobs)
        candidate = candidate_jobs[job_id]
        candidate["results"].append(
            {
                "id": track_id,
                "provider": provider,
                "key": key,
                "title": track.get("title", ""),
                "ok": bool(ok),
                "error": _sanitize_public_text(error, fallback="operation failed"),
                "quality": quality,
            }
        )
        candidate["done"] += 1
        if not ok:
            candidate["failed"] += 1
        candidate["current"] = None
        _write_jobs_candidate_locked(candidate_jobs)
        _publish_jobs_locked(candidate_jobs)


def mark_terminal(job_id: str, *, outcome: str, error: dict | None = None, _generation: int | None = None) -> None:
    if outcome not in _TERMINAL_OUTCOMES:
        raise RuntimeError("invalid terminal outcome")
    with _lock:
        _check_generation_locked(_generation)
        job = _jobs[job_id]
        if job["state"] == "done":
            raise RuntimeError("terminal job cannot transition again")
        candidate_jobs = copy.deepcopy(_jobs)
        candidate = candidate_jobs[job_id]
        candidate["state"] = "done"
        candidate["outcome"] = outcome
        candidate["current"] = None
        candidate["terminal_error"] = _sanitize_terminal_error(copy.deepcopy(error), outcome=outcome)
        _write_jobs_candidate_locked(candidate_jobs)
        _publish_jobs_locked(candidate_jobs)


def mark_current(job_id: str, title: object, *, _generation: int | None = None) -> None:
    with _lock:
        _check_generation_locked(_generation)
        if job_id not in _jobs or _jobs[job_id].get("state") != "running":
            return
        candidate_jobs = copy.deepcopy(_jobs)
        candidate_jobs[job_id]["current"] = title if isinstance(title, str) else None
        _write_jobs_candidate_locked(candidate_jobs)
        _publish_jobs_locked(candidate_jobs)


def _set_track(pl_dir, tid, **kw):
    update_track_status(pl_dir, tid, kw)


def _wav_mode() -> str:
    return load_config().get("wav_mode", "source")


def _numbering_on() -> bool:
    return bool(load_config().get("numbering", True))


def _final_download_path(pl_dir: Path, t: dict, stage_path: Path, job: dict, counter: dict) -> tuple[Path, int | None]:
    final = final_path_from_stage(stage_path)
    if not _numbering_on():
        return final, None
    ext = final.suffix.lstrip(".")
    if job.get("mode") == "playlist_order" and t.get("position"):
        num = int(t["position"])
    else:
        counter["n"] += 1
        num = counter["base"] + counter["n"]
    return Path(pl_dir) / numbered_name(num, counter["digits"], t.get("artist", ""), t["title"], ext), num


def _wav_step(fpath: Path, reference_duration: float):
    from .converter import convert_to_wav

    stage = None
    try:
        stage = convert_to_wav(fpath)
        v_ok, v_err, _ = verify_file(stage, reference_duration, tolerance=0.5)
        if not v_ok:
            cleanup_owned_stages(stage)
            return None, _public_error("conversion")
        final = publish_staged_file(stage, final_path_from_stage(stage))
        return final, ""
    except Exception:
        cleanup_owned_stages(stage)
        return None, _public_error("conversion")


def _verify_after_tags(fpath: Path, expected: int, infos_duration: int, provider: str) -> tuple[bool, str, float]:
    tol = 12.0 if (provider == "sc" and fpath.suffix.lower() == ".m4a") else 2.0
    return verify_file(fpath, expected or infos_duration, tolerance=tol)


def _public_error(kind: str) -> str:
    return {
        "download": "download failed",
        "validation": "media validation failed",
        "metadata": "metadata tagging failed",
        "conversion": "conversion failed",
        "publication": "publication failed",
    }.get(kind, "operation failed")


def _publish_wav_delete_state(pl_dir: Path, tid: str, source: Path, provider: str, entry: dict) -> None:
    next_entry = dict(entry)
    next_entry["provider"] = provider
    _set_track(pl_dir, tid, **next_entry, source_deleted=False)
    try:
        source.unlink(missing_ok=True)
    except Exception:
        return
    _set_track(pl_dir, tid, source_deleted=True, provider=provider)


def _process_track(job, pl_dir, t, ds_holder, counter):
    from .tagger import write_tags, _meta_from_deezer, _meta_from_sc

    tid = str(t["id"])
    provider = t.get("provider", "deezer")
    expected = int(t.get("duration") or 0)
    stage_path: Path | None = None

    prev = load_sidecar(pl_dir).get("tracks", {}).get(track_key(tid, provider), {})
    if prev.get("status") == "verify_failed_convert":
        src = Path(pl_dir) / prev.get("source_file", "")
        if src.exists() and src.suffix.lower() != ".wav":
            src_actual = prev.get("duration_actual") or expected
            wav, werr = _wav_step(src, src_actual)
            if wav:
                if _wav_mode() == "wav_delete":
                    _publish_wav_delete_state(
                        pl_dir,
                        tid,
                        src,
                        provider,
                        {"file": wav.name, "format": "wav", "status": "ok", "error": "", "converted_at": _now()},
                    )
                else:
                    _set_track(pl_dir, tid, file=wav.name, format="wav",
                               status="ok", error="", converted_at=_now(),
                               source_deleted=False, provider=provider)
                return True, "", "wav"
            _set_track(pl_dir, tid, status="verify_failed_convert", error=werr, provider=provider)
            return False, werr, "wav"

    ok, err, quality, actual = False, "", "", 0.0
    infos_duration = expected
    meta = None
    for attempt in range(1 + AUTO_RETRIES):
        try:
            if stage_path:
                cleanup_owned_stages(stage_path)
                stage_path = None
            if provider == "sc":
                from .soundcloud import download_track as sc_download

                stage_path, quality, sc_dur, sc_info = sc_download(t, pl_dir)
                infos_duration = int(sc_dur or expected)
                meta = _meta_from_sc(sc_info, None)
            else:
                if ds_holder["ds"] is None:
                    ds_holder["ds"] = get_session()
                stage_path, quality, infos = ds_holder["ds"].download_track(tid, pl_dir, prefer="FLAC")
                infos_duration = int(infos["DURATION"])
                meta = _meta_from_deezer(infos)
            v_ok, v_err, actual = _verify_after_tags(stage_path, expected, infos_duration, provider)
            if not v_ok:
                err = _public_error("validation")
                continue
            if meta:
                try:
                    write_tags(stage_path, meta)
                except Exception:
                    err = _public_error("metadata")
                    cleanup_owned_stages(stage_path)
                    _set_track(pl_dir, tid, title=t["title"], artist=t["artist"],
                               file="", format=str(quality).lower(),
                               status="verify_failed_metadata", error=err,
                               downloaded_at=_now(), provider=provider, url=t.get("url", ""))
                    return False, err, quality
            v_ok, v_err, actual = _verify_after_tags(stage_path, expected, infos_duration, provider)
            if v_ok:
                ok, err = True, ""
                break
            err = _public_error("validation")
        except Exception:
            err = _public_error("download")

    if not ok or stage_path is None:
        cleanup_owned_stages(stage_path)
        if "DRM" in str(err):
            err = "SoundCloud DRM-protected track is not downloadable"
        _set_track(pl_dir, tid, title=t["title"], artist=t["artist"],
                   file="", format=str(quality).lower(),
                   status="verify_failed_download", error=err,
                   downloaded_at=_now(), provider=provider, url=t.get("url", ""))
        return False, err, quality

    try:
        final_path, num = _final_download_path(pl_dir, t, stage_path, job, counter)
        published = publish_staged_file(stage_path, final_path)
    except Exception:
        cleanup_owned_stages(stage_path)
        _set_track(pl_dir, tid, title=t["title"], artist=t["artist"], file="",
                   format=str(quality).lower(), status="verify_failed_download",
                   error=_public_error("publication"), downloaded_at=_now(), provider=provider, url=t.get("url", ""))
        return False, _public_error("publication"), quality

    src_format = published.suffix.lstrip(".").lower()
    source_file_name = published.name
    base_entry = dict(title=t["title"], artist=t["artist"],
                      downloaded_at=_now(), duration_expected=expected,
                      duration_actual=round(actual, 1),
                      provider=provider, url=t.get("url", ""),
                      source_format=src_format,
                      mp3_source=src_format in ("mp3", "m4a", "aac"))
    if num:
        base_entry["position"] = num
    if _wav_mode() != "source" and src_format != "wav":
        wav, werr = _wav_step(published, actual)
        if wav is None:
            _set_track(pl_dir, tid, **base_entry, file=published.name, format=src_format,
                       source_file=source_file_name, status="verify_failed_convert", error=werr)
            return False, werr, quality
        if _wav_mode() == "wav_delete":
            _publish_wav_delete_state(
                pl_dir,
                tid,
                published,
                provider,
                {
                    **base_entry,
                    "file": wav.name,
                    "format": "wav",
                    "source_file": source_file_name,
                    "status": "ok",
                    "error": "",
                    "converted_at": _now(),
                },
            )
        else:
            _set_track(pl_dir, tid, **base_entry, file=wav.name, format="wav",
                       source_file=source_file_name,
                       source_deleted=False,
                       status="ok", error="", converted_at=_now())
        return True, "", "wav"

    _set_track(pl_dir, tid, **base_entry, file=published.name, format=src_format,
               status="ok", error="")
    return True, "", quality


def _worker(generation: int, stop_event: threading.Event):
    while not stop_event.is_set():
        with _lock:
            if generation != _worker_generation:
                return
            item = _queue.pop(0) if _queue else None
        if item is None:
            time.sleep(1)
            continue
        job_id = item
        try:
            mark_running(job_id, _generation=generation)
            with _lock:
                _check_generation_locked(generation)
                job = _jobs[job_id]
                tracks = pending_track_ids(job_id)
                job_snapshot = copy.deepcopy(job)
            pl_dir = playlist_dir(job_snapshot["playlist_id"], job_snapshot["title"])
            pl_dir.mkdir(parents=True, exist_ok=True)
            ds_holder = {"ds": None}
            pl_total = int(tracks[0].get("total") or len(tracks)) if tracks else len(tracks)
            counter = {"base": max_position(load_sidecar(pl_dir).get("tracks", {})),
                       "n": 0, "digits": 3 if pl_total >= 100 else 2}
            for t in tracks:
                mark_current(job_id, t.get("title"), _generation=generation)
                try:
                    ok, err, quality = _process_track(job_snapshot, pl_dir, t, ds_holder, counter)
                except Exception:
                    ok, err, quality = False, _public_error("download"), ""
                mark_item_complete(job_id, t, ok=ok, error=err, quality=quality, _generation=generation)
            with _lock:
                _check_generation_locked(generation)
                failed = _jobs[job_id]["failed"]
                total = _jobs[job_id]["total"]
            outcome = "succeeded" if failed == 0 else "failed" if failed == total else "partial_failure"
            mark_terminal(job_id, outcome=outcome, error=None if outcome == "succeeded" else {
                "code": outcome,
                "message": "One or more tracks failed",
            }, _generation=generation)
        except _StaleWorker:
            return
        except Exception:
            try:
                mark_terminal(
                    job_id,
                    outcome="failed",
                    error={"code": "worker_fatal", "message": "Worker failed"},
                    _generation=generation,
                )
            except Exception:
                pass


def retry_track(playlist_id: str, playlist_title: str, track: dict) -> str:
    return enqueue(playlist_id, playlist_title, [track])


FILETYPE_BY_EXT = {".mp3": 1, ".flac": 5, ".m4a": 4, ".wav": 11, ".aiff": 12}


def enqueue_flip(
    playlist_key: str,
    playlist_title: str,
    to_wav: bool,
    workers: int = 0,
    *,
    start_worker: bool | None = None,
) -> str:
    _require_initialized()
    if start_worker is not False:
        raise RuntimeError("Rekordbox mutation jobs are disabled until Task 5")
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "playlist_id": playlist_key,
        "title": playlist_title,
        "total": 0,
        "done": 0,
        "failed": 0,
        "current": None,
        "state": "queued",
        "outcome": "pending",
        "mode": "flip_to_wav" if to_wav else "flip_to_source",
        "results": [],
        "tracks": [],
        "created_at": _now(),
        "terminal_error": None,
        "to_wav": to_wav,
        "workers": workers,
    }
    with _lock:
        candidate_jobs = copy.deepcopy(_jobs)
        candidate_jobs[job_id] = job
        _write_jobs_candidate_locked(candidate_jobs)
        _publish_jobs_locked(candidate_jobs)
    should_start = _start_worker_default if start_worker is None else start_worker
    if should_start:
        threading.Thread(target=_flip_worker, args=(job_id, to_wav, workers), daemon=True).start()
    return job_id


def _flip_worker(job_id: str, to_wav: bool, workers: int):
    from concurrent.futures import ThreadPoolExecutor
    from .converter import convert_to_wav
    from . import rekordbox as rb

    try:
        mark_running(job_id)
        with _lock:
            job = _jobs[job_id]
        pl_dir = playlist_dir(job["playlist_id"], job["title"])
        sc = load_sidecar(pl_dir)
        tracks = {tid: e for tid, e in sc.get("tracks", {}).items() if is_ready_entry(pl_dir, e)}

        plan = []
        for tid, e in tracks.items():
            src = pl_dir / e["file"]
            flipped = e.get("flipped_to") == "wav"
            if to_wav and not flipped and src.suffix.lower() in (".flac", ".mp3", ".m4a", ".aac"):
                plan.append((tid, e, src, src.with_suffix(".wav")))
            elif not to_wav and flipped:
                plan.append((tid, e, src.with_suffix(".wav"), src))
        with _lock:
            job["total"] = len(plan)
            _persist_locked()
        if not plan:
            with _lock:
                job["results"] = [{"id": "-", "title": "nothing to do", "ok": True, "error": "", "quality": ""}]
                _persist_locked()
            mark_terminal(job_id, outcome="succeeded")
            return

        if rb.rb_running():
            with _lock:
                job["results"] = [{"id": "-", "title": "Rekordbox is running", "ok": False,
                                   "error": "Rekordbox is running", "quality": ""}]
                job["failed"] = len(plan)
                job["done"] = len(plan)
                _persist_locked()
            mark_terminal(job_id, outcome="failed", error={"code": "rekordbox_running", "message": "Rekordbox is running"})
            return

        converted = {}
        if to_wav:
            workers = workers or min(8, (os.cpu_count() or 4))

            def _conv(item):
                tid, e, src, _dst = item
                stage = None
                try:
                    from mutagen import File as MutagenFile

                    ref = float(MutagenFile(str(src)).info.length)
                    stage = convert_to_wav(src)
                    v_ok, _v_err, _ = verify_file(stage, ref, tolerance=0.5)
                    if not v_ok:
                        cleanup_owned_stages(stage)
                        return tid, None, _public_error("conversion")
                    wav = publish_staged_file(stage, final_path_from_stage(stage))
                    return tid, wav, ""
                except Exception:
                    cleanup_owned_stages(stage)
                    return tid, None, _public_error("conversion")

            with ThreadPoolExecutor(max_workers=workers) as ex:
                for tid, wav, err in ex.map(_conv, plan):
                    converted[tid] = (wav, err)
                    e = tracks[tid]
                    mark_item_complete(job_id, {"id": tid, "title": e["title"]},
                                       ok=wav is not None, error=err, quality="wav" if wav else "")
        else:
            for tid, e, wav, src in plan:
                exists = wav.exists()
                converted[tid] = (wav if exists else None, "" if exists else "WAV not found")
                mark_item_complete(job_id, {"id": tid, "title": e["title"]},
                                   ok=exists, error="" if exists else "WAV not found", quality="")

        backup = rb.backup_db()
        db = rb.open_db()
        rb_updated = 0
        try:
            by_path = {}
            for c in db.get_content():
                if c.FolderPath:
                    by_path[os.path.normcase(str(c.FolderPath))] = c
            for tid, e, old_rb, new_rb in plan:
                wav, _ = converted.get(tid, (None, ""))
                target = new_rb if (to_wav and wav) or not to_wav else None
                if target is None:
                    continue
                content = by_path.get(os.path.normcase(str(old_rb)))
                if content is None:
                    continue
                try:
                    db.update_content_path(content, target, save=True, commit=False)
                except Exception:
                    new_p = str(target).replace("\\", "/")
                    old_p = content.FolderPath
                    if content.OrgFolderPath == old_p:
                        content.OrgFolderPath = new_p
                    content.FolderPath = new_p
                    content.FileNameL = new_p.split("/")[-1]
                content.FileType = FILETYPE_BY_EXT.get(target.suffix.lower(), content.FileType)
                content.FileSize = target.stat().st_size
                rb_updated += 1
            db.commit()
            sc = load_sidecar(pl_dir)
            for tid, e, old_rb, new_rb in plan:
                wav, err = converted.get(tid, (None, ""))
                if to_wav and wav:
                    sc["tracks"][tid]["flipped_to"] = "wav"
                elif not to_wav and wav is not None:
                    sc["tracks"][tid].pop("flipped_to", None)
            save_sidecar(pl_dir, sc)
            with _lock:
                job["backup"] = str(backup)
                job["rb_updated"] = rb_updated
                _persist_locked()
        except Exception:
            db.rollback()
            with _lock:
                job["error"] = "master.db write failed"
                _persist_locked()
        finally:
            db.close()
        with _lock:
            failed = _jobs[job_id]["failed"]
            total = _jobs[job_id]["total"]
        outcome = "succeeded" if failed == 0 else "failed" if failed == total else "partial_failure"
        mark_terminal(job_id, outcome=outcome, error=None if outcome == "succeeded" else {
            "code": outcome,
            "message": "One or more flip items failed",
        })
    except Exception:
        mark_terminal(job_id, outcome="failed", error={"code": "worker_fatal", "message": "Worker failed"})
