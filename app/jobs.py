# -*- coding: utf-8 -*-
"""Очередь загрузок: фоновый воркер, авто-ретрай, прогресс, статусы в sidecar."""
import threading
import time
import uuid
from pathlib import Path

from .deezer_client import get_session, verify_file
from .library import playlist_dir, load_sidecar, save_sidecar

AUTO_RETRIES = 2
_jobs = {}
_lock = threading.Lock()
_queue = []
_worker_started = False


def _now():
    return int(time.time())


def enqueue(playlist_id: str, playlist_title: str, tracks: list) -> str:
    """tracks: [{id, title, artist, duration}] -> job_id"""
    global _worker_started
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id, "playlist_id": playlist_id, "title": playlist_title,
        "total": len(tracks), "done": 0, "failed": 0, "current": None,
        "state": "queued",  # queued|running|done
        "results": [],      # {id, title, ok, error, quality}
        "created_at": _now(),
    }
    with _lock:
        _jobs[job_id] = job
        _queue.append((job_id, tracks))
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=_worker, daemon=True).start()
    return job_id


def get_job(job_id: str):
    return _jobs.get(job_id)


def list_jobs():
    return sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)[:20]


def _set_track(pl_dir, tid, **kw):
    sc = load_sidecar(pl_dir)
    entry = sc.setdefault("tracks", {}).get(str(tid), {})
    entry.update(kw)
    sc["tracks"][str(tid)] = entry
    save_sidecar(pl_dir, sc)


def _worker():
    while True:
        with _lock:
            item = _queue.pop(0) if _queue else None
        if item is None:
            time.sleep(1)
            continue
        job_id, tracks = item
        job = _jobs[job_id]
        job["state"] = "running"
        pl_dir = playlist_dir(job["playlist_id"], job["title"])
        pl_dir.mkdir(parents=True, exist_ok=True)
        ds = None  # deezer-сессия — лениво, только если есть deezer-треки

        for t in tracks:
            job["current"] = t["title"]
            tid = str(t["id"])
            provider = t.get("provider", "deezer")
            expected = int(t.get("duration") or 0)
            ok, err, quality, fpath = False, "", "", None
            for attempt in range(1 + AUTO_RETRIES):
                try:
                    if provider == "sc":
                        from .soundcloud import download_track as sc_download
                        fpath, quality, sc_dur = sc_download(t, pl_dir)
                        infos_duration = int(sc_dur or expected)
                    else:
                        if ds is None:
                            ds = get_session()
                        fpath, quality, infos = ds.download_track(tid, pl_dir, prefer="FLAC")
                        infos_duration = int(infos["DURATION"])
                    # HLS-AAC со SoundCloud может плавать по длительности на неск. секунд
                    tol = 12.0 if (provider == "sc" and fpath.suffix.lower() == ".m4a") else 2.0
                    v_ok, v_err, actual = verify_file(fpath, expected or infos_duration, tolerance=tol)
                    if v_ok:
                        ok, err = True, ""
                        _set_track(pl_dir, tid, title=t["title"], artist=t["artist"],
                                   file=fpath.name, format=quality.lower(),
                                   status="ok", downloaded_at=_now(),
                                   duration_expected=expected, duration_actual=round(actual, 1),
                                   provider=provider, url=t.get("url", ""))
                        break
                    err = v_err
                except Exception as e:
                    err = str(e)
            if not ok:
                _set_track(pl_dir, tid, title=t["title"], artist=t["artist"],
                           file=fpath.name if fpath else "", format=quality.lower(),
                           status="verify_failed_download", error=err,
                           downloaded_at=_now(), provider=provider, url=t.get("url", ""))
                job["failed"] += 1
            job["results"].append({"id": tid, "title": t["title"], "ok": ok,
                                   "error": err, "quality": quality})
            job["done"] += 1
        job["current"] = None
        job["state"] = "done"


def retry_track(playlist_id: str, playlist_title: str, track: dict) -> str:
    """Ручной перезапуск одного трека из листа ошибок."""
    return enqueue(playlist_id, playlist_title, [track])
