# -*- coding: utf-8 -*-
"""SoundCloud-провайдер через yt-dlp: резолв плейлистов/страниц и скачивание."""
import time
from pathlib import Path

import yt_dlp
import imageio_ffmpeg

from .deezer_client import sanitize_filename

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

_cache = {}  # url -> (ts, data)
TTL = 60


def resolve(url: str, use_cache: bool = True) -> dict:
    """URL (сет / страница юзера / tracks / likes) -> {id, title, tracks[]}.
    Полный резолв (не flat) — иначе у треков нет title/duration/uploader."""
    if use_cache and url in _cache and time.time() - _cache[url][0] < TTL:
        return _cache[url][1]
    opts = {"quiet": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    tracks = []
    entries = info.get("entries")
    if entries is None:  # одиночный трек
        entries = [info]
    for e in entries:
        if not e:
            continue
        tracks.append({
            "id": str(e.get("id")),
            "title": e.get("title") or "?",
            "artist": e.get("uploader") or "",
            "album": "",
            "duration": int(e.get("duration") or 0),
            "url": e.get("webpage_url") or e.get("url") or url,
        })
    data = {
        "id": str(info.get("id") or abs(hash(url))),
        "title": info.get("title") or url,
        "tracks": tracks,
    }
    _cache[url] = (time.time(), data)
    return data


def download_track(track: dict, out_dir: Path):
    """Скачивает трек по url. Возвращает (path, format, actual_duration)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(f"{track['artist']} - {track['title']}" if track.get("artist")
                             else track["title"])
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        # предпочитаем progressive MP3 (точная длительность, чистый контейнер),
        # затем AAC 160k (HLS — возможен сдвиг длительности на неск. секунд)
        "format": "http_mp3_1_0/hls_mp3_1_0/hls_aac_160k/bestaudio/best",
        "outtmpl": str(out_dir / (base + ".%(ext)s")),
        "ffmpeg_location": FFMPEG,
        "postprocessor_args": ["-movflags", "+faststart"],
    }
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(track["url"], download=True)
        fpath = Path(y.prepare_filename(info))
    # yt-dlp может поменять расширение после пост-обработки
    if not fpath.exists():
        cands = sorted(out_dir.glob(base + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
        fpath = cands[0]
    return fpath, fpath.suffix.lstrip(".").lower(), float(info.get("duration") or 0), info
