# -*- coding: utf-8 -*-
"""SoundCloud-провайдер через yt-dlp: резолв плейлистов/страниц и скачивание.
Плюс авторизация по oauth_token (cookie) — плейлисты и лайки аккаунта."""
import time
from pathlib import Path

import requests
import yt_dlp
import imageio_ffmpeg

from .deezer_client import sanitize_filename, load_config, save_config

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SC_API = "https://api-v2.soundcloud.com"

_cache = {}  # url -> (ts, data)
TTL = 60


# ---------- авторизация ----------

def sc_oauth_token() -> str | None:
    return load_config().get("sc_oauth")


def sc_validate(token: str) -> dict:
    """Проверяет oauth_token на /me. Возвращает данные юзера."""
    r = requests.get(f"{SC_API}/me", headers={"Authorization": f"OAuth {token}"}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"токен не принят (HTTP {r.status_code})")
    return r.json()


def sc_account_playlists(token: str) -> list:
    """Плейлисты аккаунта: [{id,title,url,count}]."""
    out, url = [], f"{SC_API}/me/playlists?limit=50&linked_partitioning=1"
    while url:
        r = requests.get(url, headers={"Authorization": f"OAuth {token}"}, timeout=20)
        r.raise_for_status()
        d = r.json()
        for p in d.get("collection", []):
            out.append({"id": str(p["id"]), "title": p.get("title") or "?",
                        "url": p.get("permalink_url"), "count": p.get("track_count", 0)})
        url = d.get("next_href")
    return out


def _oauth_cookiefile() -> str | None:
    """Netscape cookie-файл с oauth_token для yt-dlp (лайки, приватное)."""
    token = sc_oauth_token()
    if not token:
        return None
    p = Path(__file__).parent.parent / ".sc_cookies.txt"
    if not p.exists() or token not in p.read_text():
        p.write_text(
            "# Netscape HTTP Cookie File\n"
            f".soundcloud.com\tTRUE\t/\tTRUE\t2000000000\toauth_token\t{token}\n")
    return str(p)


def resolve(url: str, use_cache: bool = True) -> dict:
    """URL (сет / страница юзера / tracks / likes) -> {id, title, tracks[]}.
    Полный резолв (не flat) — иначе у треков нет title/duration/uploader."""
    if use_cache and url in _cache and time.time() - _cache[url][0] < TTL:
        return _cache[url][1]
    opts = {"quiet": True, "ignoreerrors": True}
    cookies = _oauth_cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
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
    cookies = _oauth_cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(track["url"], download=True)
        fpath = Path(y.prepare_filename(info))
    # yt-dlp может поменять расширение после пост-обработки
    if not fpath.exists():
        cands = sorted(out_dir.glob(base + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
        fpath = cands[0]
    return fpath, fpath.suffix.lstrip(".").lower(), float(info.get("duration") or 0), info
