# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import os
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
from .library import load_sidecar, max_position, numbered_name, playlist_dir, save_sidecar, track_key, update_track_status

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
        clean["jobs"][job_id] = dict(job)
    return clean


def initialize(data_root: Path | None = None, *, start_worker: bool = True, write_json=None) -> None:
    global _initialized, _journal_path, _start_worker_default, _write_json, _jobs, _queue
    if data_root is None:
        raise RuntimeError("DeckPipe jobs require an explicit data root")
    with _lock:
        _journal_path = Path(data_root) / "jobs.json"
        _start_worker_default = start_worker
        _write_json = write_json or atomic_write_json
        payload, recovered = atomic_load_json(
            _journal_path,
            default={"version": JOURNAL_VERSION, "jobs": {}},
            validator=_validate_journal,
            backup=True,
            return_recovered=True,
        )
        _jobs = {job_id: _normalize_job(job) for job_id, job in payload["jobs"].items()}
        _queue = []
        changed = False
        for job_id, job in _jobs.items():
            if job.get("state") in ("queued", "running"):
                if job.get("mode") in IDEMPOTENT_DOWNLOAD_MODES:
                    job["state"] = "queued"
                    job["outcome"] = "pending"
                    job["current"] = None
                    if pending_track_ids(job_id):
                        _queue.append(job_id)
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
        _initialized = True
        if changed:
            _persist_locked(backup=not recovered)
        if start_worker and _queue:
            _ensure_worker_locked()


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


def _require_initialized() -> None:
    if not _initialized or _journal_path is None:
        raise RuntimeError("DeckPipe jobs are not initialized")


def _snapshot(job: dict | None):
    return copy.deepcopy(job) if job is not None else None


def _persist_locked(*, backup: bool = True) -> None:
    _require_initialized()
    payload = {"version": JOURNAL_VERSION, "jobs": _jobs}
    _write_json(_journal_path, payload, validator=_validate_journal, backup=backup)


def _ensure_worker_locked() -> None:
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_worker, daemon=True).start()


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
    job = {
        "id": job_id,
        "playlist_id": playlist_id,
        "title": playlist_title,
        "total": len(tracks),
        "done": 0,
        "failed": 0,
        "current": None,
        "state": "queued",
        "outcome": "pending",
        "mode": mode,
        "results": [],
        "tracks": [dict(t) for t in tracks],
        "created_at": _now(),
        "terminal_error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _queue_job_locked(job_id, start_worker=start_worker)
        _persist_locked()
    return job_id


def get_job(job_id: str):
    with _lock:
        return _snapshot(_jobs.get(job_id))


def list_jobs():
    with _lock:
        return copy.deepcopy(sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)[:20])


def pending_track_ids(job_id: str) -> list[dict]:
    job = _jobs[job_id]
    completed = {str(item.get("id")) for item in job.get("results", [])}
    return [dict(t) for t in job.get("tracks", []) if str(t.get("id")) not in completed]


def mark_running(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        if job["state"] == "done":
            raise RuntimeError("terminal job cannot run again")
        if job["state"] == "queued":
            job["state"] = "running"
            job["outcome"] = "pending"
            _persist_locked()


def mark_item_complete(job_id: str, track: dict, *, ok: bool, error: str, quality: str) -> None:
    with _lock:
        job = _jobs[job_id]
        if job["state"] == "done":
            raise RuntimeError("terminal job cannot accept progress")
        track_id = str(track["id"])
        if not any(str(item.get("id")) == track_id for item in job["results"]):
            job["results"].append(
                {"id": track_id, "title": track.get("title", ""), "ok": bool(ok), "error": error, "quality": quality}
            )
            job["done"] += 1
            if not ok:
                job["failed"] += 1
        job["current"] = None
        _persist_locked()


def mark_terminal(job_id: str, *, outcome: str, error: dict | None = None) -> None:
    with _lock:
        job = _jobs[job_id]
        job["state"] = "done"
        job["outcome"] = outcome
        job["current"] = None
        job["terminal_error"] = copy.deepcopy(error)
        _persist_locked()


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
            return None, f"wav after conversion: {v_err}"
        final = publish_staged_file(stage, final_path_from_stage(stage))
        return final, ""
    except Exception as e:
        cleanup_owned_stages(stage)
        return None, f"conversion: {e}"


def _verify_after_tags(fpath: Path, expected: int, infos_duration: int, provider: str) -> tuple[bool, str, float]:
    tol = 12.0 if (provider == "sc" and fpath.suffix.lower() == ".m4a") else 2.0
    return verify_file(fpath, expected or infos_duration, tolerance=tol)


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
                    src.unlink(missing_ok=True)
                _set_track(pl_dir, tid, file=wav.name, format="wav",
                           status="ok", error="", converted_at=_now(),
                           source_deleted=_wav_mode() == "wav_delete", provider=provider)
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
                err = v_err
                continue
            if meta:
                try:
                    write_tags(stage_path, meta)
                except Exception:
                    pass
            v_ok, v_err, actual = _verify_after_tags(stage_path, expected, infos_duration, provider)
            if v_ok:
                ok, err = True, ""
                break
            err = v_err
        except Exception as e:
            err = str(e)

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
    except Exception as e:
        cleanup_owned_stages(stage_path)
        _set_track(pl_dir, tid, title=t["title"], artist=t["artist"], file="",
                   format=str(quality).lower(), status="verify_failed_download",
                   error=str(e), downloaded_at=_now(), provider=provider, url=t.get("url", ""))
        return False, str(e), quality

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
            published.unlink(missing_ok=True)
        _set_track(pl_dir, tid, **base_entry, file=wav.name, format="wav",
                   source_file=source_file_name,
                   source_deleted=_wav_mode() == "wav_delete",
                   status="ok", error="", converted_at=_now())
        return True, "", "wav"

    _set_track(pl_dir, tid, **base_entry, file=published.name, format=src_format,
               status="ok", error="")
    return True, "", quality


def _worker():
    while True:
        with _lock:
            item = _queue.pop(0) if _queue else None
        if item is None:
            time.sleep(1)
            continue
        job_id = item
        try:
            mark_running(job_id)
            with _lock:
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
                with _lock:
                    _jobs[job_id]["current"] = t.get("title")
                    _persist_locked()
                try:
                    ok, err, quality = _process_track(job_snapshot, pl_dir, t, ds_holder, counter)
                except Exception as e:
                    ok, err, quality = False, f"internal error: {e}", ""
                mark_item_complete(job_id, t, ok=ok, error=err, quality=quality)
            with _lock:
                failed = _jobs[job_id]["failed"]
                total = _jobs[job_id]["total"]
            outcome = "succeeded" if failed == 0 else "failed" if failed == total else "partial_failure"
            mark_terminal(job_id, outcome=outcome, error=None if outcome == "succeeded" else {
                "code": outcome,
                "message": "One or more tracks failed",
            })
        except Exception as e:
            try:
                mark_terminal(job_id, outcome="failed", error={"code": "worker_fatal", "message": str(e)})
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
        _jobs[job_id] = job
        _persist_locked()
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
        tracks = {tid: e for tid, e in sc.get("tracks", {}).items()
                  if e.get("status") == "ok" and e.get("file") and not is_partial_path(e["file"])
                  and (pl_dir / e["file"]).exists()}

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
                    v_ok, v_err, _ = verify_file(stage, ref, tolerance=0.5)
                    if not v_ok:
                        cleanup_owned_stages(stage)
                        return tid, None, v_err
                    wav = publish_staged_file(stage, final_path_from_stage(stage))
                    return tid, wav, ""
                except Exception as ex:
                    cleanup_owned_stages(stage)
                    return tid, None, str(ex)

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
        except Exception as ex:
            db.rollback()
            with _lock:
                job["error"] = f"master.db write failed: {ex}"
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
    except Exception as e:
        mark_terminal(job_id, outcome="failed", error={"code": "worker_fatal", "message": str(e)})
