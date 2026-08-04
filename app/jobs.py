# -*- coding: utf-8 -*-
"""Очередь загрузок: фоновый воркер, авто-ретрай, прогресс, статусы в sidecar."""
import threading
import time
import uuid
from pathlib import Path

from .deezer_client import get_session, verify_file, load_config
from .library import playlist_dir, load_sidecar, save_sidecar

AUTO_RETRIES = 2
_jobs = {}
_lock = threading.Lock()
_queue = []
_worker_started = False


def _now():
    return int(time.time())


def enqueue(playlist_id: str, playlist_title: str, tracks: list, mode: str = "append") -> str:
    """tracks: [{id, title, artist, duration, position?}] -> job_id
    mode: 'append' (новые вниз) | 'playlist_order' (номер = позиция в плейлисте)"""
    global _worker_started
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id, "playlist_id": playlist_id, "title": playlist_title,
        "total": len(tracks), "done": 0, "failed": 0, "current": None,
        "state": "queued", "mode": mode,
        "results": [],
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


def _wav_mode() -> str:
    return load_config().get("wav_mode", "source")  # source|wav|wav_delete


def _numbering_on() -> bool:
    return bool(load_config().get("numbering", True))


def _apply_numbering(pl_dir, t, fpath, job, counter):
    """Переименовывает файл в 'NN - Artist - Title.ext' по режиму синка.
    Возвращает (fpath, num|None)."""
    if not _numbering_on():
        return fpath, None
    from .library import numbered_name
    ext = fpath.suffix.lstrip(".")
    if job.get("mode") == "playlist_order" and t.get("position"):
        num = int(t["position"])
    else:  # append: продолжаем с максимального существующего номера
        counter["n"] += 1
        num = counter["base"] + counter["n"]
    new_name = numbered_name(num, counter["digits"], t.get("artist", ""), t["title"], ext)
    if fpath.name != new_name:
        new = fpath.with_name(new_name)
        if not new.exists():
            fpath.rename(new)
            fpath = new
    return fpath, num


def _wav_step(fpath: Path, reference_duration: float):
    """Конвертация в WAV + верификация (сверка с длительностью исходника).
    Возвращает (wav_path, error)."""
    from .converter import convert_to_wav
    try:
        wav = convert_to_wav(fpath)
    except Exception as e:
        return None, f"конвертация: {e}"
    v_ok, v_err, _ = verify_file(wav, reference_duration, tolerance=0.5)
    if not v_ok:
        return None, f"wav после конвертации: {v_err}"
    return wav, ""


def _process_track(job, pl_dir, t, ds_holder, counter):
    """Полный пайплайн одного трека: (пере)скачивание -> теги -> нумерация -> WAV-режим."""
    from .tagger import write_tags, _meta_from_deezer, _meta_from_sc
    tid = str(t["id"])
    provider = t.get("provider", "deezer")
    expected = int(t.get("duration") or 0)

    # ретрай ошибки КОНВЕРТАЦИИ: исходник на диске — только переконвертируем
    prev = load_sidecar(pl_dir).get("tracks", {}).get(tid, {})
    if prev.get("status") == "verify_failed_convert":
        src = pl_dir / prev.get("source_file", "")
        if src.exists() and src.suffix.lower() != ".wav":
            src_actual = prev.get("duration_actual") or expected
            wav, werr = _wav_step(src, src_actual)
            if wav:
                if _wav_mode() == "wav_delete":
                    src.unlink(missing_ok=True)
                _set_track(pl_dir, tid, file=wav.name, format="wav",
                           status="ok", error="", converted_at=_now(),
                           source_deleted=_wav_mode() == "wav_delete")
                return True, "", "wav"
            _set_track(pl_dir, tid, status="verify_failed_convert", error=werr)
            return False, werr, "wav"

    ok, err, quality, fpath = False, "", "", None
    meta = None
    for attempt in range(1 + AUTO_RETRIES):
        try:
            if provider == "sc":
                from .soundcloud import download_track as sc_download
                fpath, quality, sc_dur, sc_info = sc_download(t, pl_dir)
                infos_duration = int(sc_dur or expected)
                meta = _meta_from_sc(sc_info, None)
            else:
                if ds_holder["ds"] is None:
                    ds_holder["ds"] = get_session()
                fpath, quality, infos = ds_holder["ds"].download_track(tid, pl_dir, prefer="FLAC")
                infos_duration = int(infos["DURATION"])
                meta = _meta_from_deezer(infos)
            # HLS-AAC со SoundCloud может плавать по длительности на неск. секунд
            tol = 12.0 if (provider == "sc" and fpath.suffix.lower() == ".m4a") else 2.0
            v_ok, v_err, actual = verify_file(fpath, expected or infos_duration, tolerance=tol)
            if v_ok:
                if meta:
                    write_tags(fpath, meta)
                ok, err = True, ""
                break
            err = v_err
        except Exception as e:
            err = str(e)

    if not ok:
        _set_track(pl_dir, tid, title=t["title"], artist=t["artist"],
                   file=fpath.name if fpath else "", format=quality.lower(),
                   status="verify_failed_download", error=err,
                   downloaded_at=_now(), provider=provider, url=t.get("url", ""))
        return False, err, quality

    # --- нумерация по выбранному режиму ---
    fpath, num = _apply_numbering(pl_dir, t, fpath, job, counter)

    # --- WAV-режим ---
    src_format = fpath.suffix.lstrip(".").lower()
    source_file_name = fpath.name
    base_entry = dict(title=t["title"], artist=t["artist"],
                      downloaded_at=_now(), duration_expected=expected,
                      duration_actual=round(actual, 1),
                      provider=provider, url=t.get("url", ""),
                      source_format=src_format,
                      mp3_source=src_format in ("mp3", "m4a", "aac"))
    if num:
        base_entry["position"] = num
    if _wav_mode() != "source" and src_format != "wav":
        wav, werr = _wav_step(fpath, actual)
        if wav is None:
            _set_track(pl_dir, tid, **base_entry, file=fpath.name, format=src_format,
                       source_file=source_file_name, status="verify_failed_convert", error=werr)
            return False, werr, quality
        if _wav_mode() == "wav_delete":
            fpath.unlink(missing_ok=True)
        _set_track(pl_dir, tid, **base_entry, file=wav.name, format="wav",
                   source_file=source_file_name,
                   source_deleted=_wav_mode() == "wav_delete",
                   status="ok", error="", converted_at=_now())
        return True, "", "wav"

    _set_track(pl_dir, tid, **base_entry, file=fpath.name, format=src_format,
               status="ok", error="")
    return True, "", quality


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
        ds_holder = {"ds": None}  # deezer-сессия — лениво
        from .library import max_position as _max_pos, digits_for as _digits
        pl_total = int(tracks[0].get("total") or len(tracks)) if tracks else len(tracks)
        counter = {"base": _max_pos(load_sidecar(pl_dir).get("tracks", {})),
                   "n": 0, "digits": _digits(pl_total)}

        for t in tracks:
            job["current"] = t["title"]
            try:
                ok, err, quality = _process_track(job, pl_dir, t, ds_holder, counter)
            except Exception as e:
                ok, err, quality = False, f"внутренняя ошибка: {e}", ""
            if not ok:
                job["failed"] += 1
            job["results"].append({"id": str(t["id"]), "title": t["title"], "ok": ok,
                                   "error": err, "quality": quality})
            job["done"] += 1
        job["current"] = None
        job["state"] = "done"


def retry_track(playlist_id: str, playlist_title: str, track: dict) -> str:
    """Ручной перезапуск одного трека из листа ошибок."""
    return enqueue(playlist_id, playlist_title, [track])
