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
TTL = 600  # 10 мин: рейт-лимит SC делает повторные резолвы дорогими


# ---------- авторизация ----------

def sc_oauth_token() -> str | None:
    return load_config().get("sc_oauth")


_CID_FALLBACK = "sUn5toeW5d8MC2jOLpE2yAibTG7RRYsA"
_cid_cache = {"id": None, "ts": 0}


def sc_client_id() -> str:
    """client_id веб-приложения SC (из JS-ассетов, кеш 6 ч, fallback — константа)."""
    import re
    if _cid_cache["id"] and time.time() - _cid_cache["ts"] < 6 * 3600:
        return _cid_cache["id"]
    try:
        UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        html = requests.get("https://soundcloud.com", headers=UA, timeout=20).text
        scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
        for s in scripts:
            if s.startswith("/"):
                s = "https://soundcloud.com" + s
            try:
                js = requests.get(s, headers=UA, timeout=20).text
            except Exception:
                continue
            m = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', js)
            if m:
                _cid_cache.update(id=m.group(1), ts=time.time())
                return _cid_cache["id"]
    except Exception:
        pass
    return _CID_FALLBACK


def _headers(token: str) -> dict:
    return {"Authorization": f"OAuth {token}"}


def _params(**kw) -> dict:
    return {"client_id": sc_client_id(), **kw}


def sc_validate(token: str) -> dict:
    """Проверяет oauth_token на /me (с ретраями при рейт-лимите)."""
    try:
        return _api_get("/me", token)
    except Exception as e:
        raise RuntimeError(f"токен не принят ({e})")


def sc_account_playlists(token: str) -> list:
    """Свои + лайкнутые плейлисты: [{id,title,url,count}].
    Толерантно к 403/рейт-лимиту: возвращает то, что удалось получить."""
    me = sc_validate(token)
    uid = me["id"]
    out = []
    # свои плейлисты
    try:
        url = f"{SC_API}/users/{uid}/playlists"
        while url:
            r = requests.get(url, headers=_headers(token),
                             params=_params(limit=50, linked_partitioning=1) if SC_API in url else None,
                             timeout=20)
            r.raise_for_status()
            d = r.json()
            for p in d.get("collection", []):
                out.append({"id": str(p["id"]), "title": p.get("title") or "?",
                            "url": p.get("permalink_url"), "count": p.get("track_count", 0)})
            url = d.get("next_href")
    except Exception:
        pass
    # лайкнутые плейлисты (библиотека)
    try:
        url = f"{SC_API}/me/library/all"
        seen = {p["id"] for p in out}
        while url:
            r = requests.get(url, headers=_headers(token),
                             params=_params(limit=50, linked_partitioning=1) if SC_API in url else None,
                             timeout=20)
            if r.status_code != 200:
                break
            d = r.json()
            for it in d.get("collection", []):
                if it.get("type") not in ("playlist-like", "playlist"):
                    continue
                p = it.get("playlist") or it
                pid = str(p.get("id", ""))
                if pid and pid not in seen and p.get("permalink_url"):
                    seen.add(pid)
                    out.append({"id": pid, "title": "♥ " + (p.get("title") or "?"),
                                "url": p["permalink_url"], "count": p.get("track_count", 0)})
            url = d.get("next_href")
    except Exception:
        pass
    return out


def _oauth_cookiefile() -> str | None:
    """Netscape cookie-файл с oauth_token для yt-dlp (лайки, приватное)."""
    token = sc_oauth_token()
    if not token:
        return None
    from .deezer_client import ROOT
    p = ROOT / ".sc_cookies.txt"
    if not p.exists() or token not in p.read_text():
        p.write_text(
            "# Netscape HTTP Cookie File\n"
            f".soundcloud.com\tTRUE\t/\tTRUE\t2000000000\toauth_token\t{token}\n")
    return str(p)


def _track_from_api(t: dict) -> dict:
    return {
        "id": str(t.get("id")),
        "title": t.get("title") or "?",
        "artist": (t.get("user") or {}).get("username", ""),
        "album": "",
        "duration": int((t.get("duration") or 0) / 1000),
        "url": t.get("permalink_url") or "",
    }


def _api_get(path: str, token: str | None, **params) -> dict:
    h = _headers(token) if token else {}
    last = None
    for attempt in range(4):
        r = requests.get(f"{SC_API}{path}", headers=h, params=_params(**params), timeout=30)
        if r.status_code == 200:
            return r.json()
        last = f"HTTP {r.status_code}"
        if r.status_code in (403, 429) and attempt < 3:
            time.sleep(5 * (attempt + 1) ** 2)  # рейт-лимит SC: 5с, 20с, 45с
            continue
        r.raise_for_status()
    raise RuntimeError(last or "ошибка API")


def search(q: str) -> dict:
    """Поиск SoundCloud: треки/сеты/юзеры через api-v2."""
    token = sc_oauth_token()
    out = {"tracks": [], "albums": [], "artists": []}
    try:
        d = _api_get("/search/tracks", token, q=q, limit=10)
        out["tracks"] = [_track_from_api(t) for t in d.get("collection", [])]
    except Exception:
        pass
    try:
        d = _api_get("/search/albums", token, q=q, limit=5)
        out["albums"] = [{"id": str(p.get("id")), "title": p.get("title") or "?",
                          "artist": (p.get("user") or {}).get("username", ""),
                          "url": p.get("permalink_url") or "",
                          "count": p.get("track_count", 0)}
                         for p in d.get("collection", [])]
    except Exception:
        pass
    try:
        d = _api_get("/search/users", token, q=q, limit=5)
        out["artists"] = [{"id": str(u.get("id")), "name": u.get("username") or "?",
                           "url": u.get("permalink_url") or ""}
                          for u in d.get("collection", [])]
    except Exception:
        pass
    return out


def resolve(url: str, use_cache: bool = True) -> dict:
    """URL (сет / страница юзера / tracks / лайки) -> {id, title, tracks[]}.
    Сеты и юзеры — через api-v2 (1-2 запроса), лайки — через yt-dlp."""
    if use_cache and url in _cache and time.time() - _cache[url][0] < TTL:
        return _cache[url][1]
    token = sc_oauth_token()

    if "/likes" in url:
        data = _resolve_likes(url)
    else:
        data = _resolve_api(url, token)
    _cache[url] = (time.time(), data)
    return data


def _resolve_api(url: str, token: str | None) -> dict:
    """api-v2 /resolve: сет (1 запрос, треки с метаданными) или юзер (+треки юзера)."""
    obj = _api_get("/resolve", token, url=url)
    kind = obj.get("kind")
    if kind == "playlist":
        return {"id": str(obj["id"]), "title": obj.get("title") or "playlist",
                "tracks": [_track_from_api(t) for t in obj.get("tracks", []) if t.get("id")]}
    if kind == "user":
        tracks, next_url = [], f"/users/{obj['id']}/tracks"
        params = _params(limit=200, linked_partitioning=1)
        while next_url:
            r = requests.get(next_url if next_url.startswith("http") else f"{SC_API}{next_url}",
                             headers=_headers(token) if token else {},
                             params=params if next_url.startswith("/") else None, timeout=30)
            r.raise_for_status()
            d = r.json()
            tracks += [_track_from_api(t) for t in d.get("collection", [])]
            next_url, params = d.get("next_href"), None
        return {"id": str(obj["id"]), "title": obj.get("username") or "user", "tracks": tracks}
    if kind == "track":
        return {"id": str(obj["id"]), "title": obj.get("title") or "track",
                "tracks": [_track_from_api(obj)]}
    raise RuntimeError(f"неизвестный тип ресурса: {kind}")


def _resolve_likes(url: str) -> dict:
    """Лайки — только yt-dlp с oauth-cookie (публичный API их закрыл).
    Затем добиваем artist/duration батчами через api-v2 /tracks?ids=."""
    cookies = _oauth_cookiefile()
    if not cookies:
        raise RuntimeError("лайки доступны после входа (SC: вход)")
    opts = {"quiet": True, "ignoreerrors": True, "extract_flat": True,
            "cookiefile": cookies}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp не смог прочитать лайки")
    tracks = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        tracks.append({
            "id": str(e.get("id")),
            "title": e.get("title") or "?",
            "artist": e.get("uploader") or "",
            "album": "",
            "duration": int(e.get("duration") or 0),
            "url": e.get("url") or e.get("webpage_url") or "",
        })
    # добивка метаданных батчами по 50 id (устойчиво к рейт-лимиту: батч не удался — пропускаем)
    token = sc_oauth_token()
    for i in range(0, len(tracks), 50):
        batch = [t for t in tracks[i:i + 50] if not t["artist"] or not t["duration"]]
        if not batch:
            continue
        try:
            ids = ",".join(t["id"] for t in batch)
            coll = _api_get("/tracks", token, ids=ids)
            if isinstance(coll, dict):
                coll = coll.get("collection", [])
            by_id = {str(t.get("id")): t for t in coll}
            for t in batch:
                full = by_id.get(t["id"])
                if full:
                    t["artist"] = (full.get("user") or {}).get("username", t["artist"])
                    t["duration"] = int((full.get("duration") or 0) / 1000)
        except Exception:
            pass
        time.sleep(0.4)  # пейсинг против рейт-лимита
    return {"id": "likes", "title": info.get("title") or "❤ Лайки", "tracks": tracks}


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
