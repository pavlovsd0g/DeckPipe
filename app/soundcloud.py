# -*- coding: utf-8 -*-
"""SoundCloud-провайдер через yt-dlp: резолв плейлистов/страниц и скачивание.
Плюс авторизация по oauth_token (cookie) — плейлисты и лайки аккаунта."""
import time
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
import imageio_ffmpeg

from .atomic_io import is_partial_path, make_staged_path
from .download_control import cancellable_ytdlp_processes, check_cancelled, cleanup_download_stages as cleanup_owned_stages, register_stage
from .deezer_client import sanitize_filename, get_soundcloud_oauth
from .provider_errors import (
    detail_from_exception,
    provider_collection_incomplete,
    provider_exception,
    provider_pagination_invalid,
)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SC_API = "https://api-v2.soundcloud.com"
_WEB_HEADERS = {
    "Origin": "https://soundcloud.com",
    "Referer": "https://soundcloud.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}

_cache = {}  # url -> (ts, data)
TTL = 600  # 10 мин: рейт-лимит SC делает повторные резолвы дорогими
PROVIDER_PAGE_LIMIT = 1000


class ProviderCollection(list):
    def __init__(self, items=(), *, errors: list[dict] | None = None, account_id: str | None = None):
        super().__init__(items)
        self.errors = list(errors or [])
        self.account_id = account_id


# ---------- авторизация ----------

def sc_oauth_token() -> str | None:
    return get_soundcloud_oauth()


_CID_FALLBACK = "sUn5toeW5d8MC2jOLpE2yAibTG7RRYsA"
_cid_cache = {"id": None, "ts": 0}


def sc_client_id() -> str:
    """client_id веб-приложения SC (из JS-ассетов, кеш 6 ч, fallback — константа)."""
    import re
    if _cid_cache["id"] and time.time() - _cid_cache["ts"] < 6 * 3600:
        return _cid_cache["id"]
    try:
        html = requests.get("https://soundcloud.com", headers=_WEB_HEADERS, timeout=20).text
        scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
        for s in scripts:
            if s.startswith("/"):
                s = "https://soundcloud.com" + s
            try:
                js = requests.get(s, headers=_WEB_HEADERS, timeout=20).text
            except Exception:
                continue
            m = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', js)
            if m:
                _cid_cache.update(id=m.group(1), ts=time.time())
                return _cid_cache["id"]
    except Exception:
        pass
    return _CID_FALLBACK


def _headers(token: str | None) -> dict:
    return {**_WEB_HEADERS, **({"Authorization": f"OAuth {token}"} if token else {})}


def _params(**kw) -> dict:
    return {"client_id": sc_client_id(), **kw}


def sc_validate(token: str) -> dict:
    """Проверяет oauth_token на /me (с ретраями при рейт-лимите)."""
    return _api_get("/me", token)


def sc_account_playlists(token: str) -> list:
    """Свои + лайкнутые плейлисты: [{id,title,url,count}].
    Частичная выдача возвращается вместе с безопасным списком errors."""
    me = sc_validate(token)
    uid = me["id"]
    out, errors = [], []
    # свои плейлисты
    try:
        for d in _account_pages(f"{SC_API}/users/{uid}/playlists", token):
            for p in d.get("collection", []):
                if not isinstance(p, dict) or p.get("id") is None:
                    raise provider_collection_incomplete("soundcloud")
                out.append({"id": str(p["id"]), "title": p.get("title") or "?",
                            "url": p.get("permalink_url"), "count": p.get("track_count", 0)})
    except Exception as exc:
        errors.append(detail_from_exception("soundcloud", exc))
    # The user's likes feed contains both track and playlist wrappers. The old
    # /me/library/all endpoint may deny access even with a valid signed-in user.
    try:
        seen = {p["id"] for p in out}
        for d in _account_pages(f"{SC_API}/users/{uid}/likes", token):
            for it in d.get("collection", []):
                if not isinstance(it, dict):
                    raise provider_collection_incomplete("soundcloud")
                if 'playlist' not in it and isinstance(it.get('track'), dict):
                    continue
                p = it.get("playlist")
                if not isinstance(p, dict) or p.get("id") is None or not p.get("permalink_url"):
                    raise provider_collection_incomplete("soundcloud")
                pid = str(p["id"])
                if pid not in seen:
                    seen.add(pid)
                    out.append({"id": pid, "title": "♥ " + (p.get("title") or "?"),
                                "url": p["permalink_url"], "count": p.get("track_count", 0)})
    except Exception as exc:
        errors.append(detail_from_exception("soundcloud", exc))
    return ProviderCollection(out, errors=errors, account_id=str(uid))


def _trusted_sc_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api-v2.soundcloud.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except Exception:
        return False


def _account_pages(first_url: str, token: str):
    url, params, seen = first_url, _params(limit=50, linked_partitioning=1), set()
    for _page_number in range(PROVIDER_PAGE_LIMIT):
        if not isinstance(url, str) or not _trusted_sc_url(url) or url in seen:
            raise provider_pagination_invalid("soundcloud")
        seen.add(url)
        try:
            r = requests.get(url, headers=_headers(token), params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            raise provider_exception("soundcloud", exc) from None
        if not isinstance(data, dict) or not isinstance(data.get("collection"), list):
            raise provider_collection_incomplete("soundcloud")
        yield data
        next_url = data.get("next_href")
        if not next_url:
            return
        if not isinstance(next_url, str) or not _trusted_sc_url(next_url):
            raise provider_pagination_invalid("soundcloud")
        url, params = next_url, None
    raise provider_collection_incomplete("soundcloud")


def _soundcloud_cookiejar(token: str):
    jar = yt_dlp.cookies.YoutubeDLCookieJar()
    jar.set_cookie(
        Cookie(
            version=0,
            name="oauth_token",
            value=token,
            port=None,
            port_specified=False,
            domain=".soundcloud.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        )
    )
    return jar


def _youtube_dl_opts_with_oauth(opts: dict, token: str | None) -> dict:
    if not token:
        return dict(opts)
    jar = _soundcloud_cookiejar(token)
    return {**opts, "cookiejar": jar}


def _bind_cookiejar(ytdl, opts: dict) -> None:
    jar = opts.get("cookiejar")
    if jar is not None:
        ytdl.__dict__["cookiejar"] = jar


def _current_stage_candidates(out_dir: Path, stage_prefix: str) -> list[Path]:
    try:
        entries = list(Path(out_dir).iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        [p for p in entries if p.name.startswith(stage_prefix) and is_partial_path(p)],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _is_current_stage_path(path: Path, out_dir: Path, stage_prefix: str) -> bool:
    path = Path(path)
    try:
        return path.parent.resolve() == Path(out_dir).resolve() and path.name.startswith(stage_prefix) and is_partial_path(path)
    except Exception:
        return False


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
    h = _headers(token)
    last = None
    for attempt in range(4):
        try:
            r = requests.get(f"{SC_API}{path}", headers=h, params=_params(**params), timeout=30)
        except Exception as exc:
            raise provider_exception("soundcloud", exc) from None
        if r.status_code == 200:
            try:
                return r.json()
            except Exception as exc:
                raise provider_exception("soundcloud", exc) from None
        last = f"HTTP {r.status_code}"
        if r.status_code in (403, 429) and attempt < 3:
            time.sleep(5 * (attempt + 1) ** 2)  # рейт-лимит SC: 5с, 20с, 45с
            continue
        try:
            r.raise_for_status()
        except Exception as exc:
            raise provider_exception("soundcloud", exc) from None
    raise provider_exception("soundcloud", RuntimeError(last or "provider request failed"))


def search(q: str) -> dict:
    """Поиск SoundCloud: треки/сеты/юзеры через api-v2."""
    token = sc_oauth_token()
    out = {"tracks": [], "albums": [], "artists": [], "errors": []}
    try:
        d = _api_get("/search/tracks", token, q=q, limit=10)
        collection, incomplete = _search_collection(d)
        out["tracks"] = [_track_from_api(t) for t in collection]
        if incomplete:
            out["errors"].append(provider_collection_incomplete("soundcloud").detail)
    except Exception as exc:
        out["errors"].append(detail_from_exception("soundcloud", exc))
    try:
        d = _api_get("/search/albums", token, q=q, limit=5)
        collection, incomplete = _search_collection(d)
        out["albums"] = [{"id": str(p.get("id")), "title": p.get("title") or "?",
                          "artist": (p.get("user") or {}).get("username", ""),
                          "url": p.get("permalink_url") or "",
                          "count": p.get("track_count", 0)}
                         for p in collection]
        if incomplete:
            out["errors"].append(provider_collection_incomplete("soundcloud").detail)
    except Exception as exc:
        out["errors"].append(detail_from_exception("soundcloud", exc))
    try:
        d = _api_get("/search/users", token, q=q, limit=5)
        collection, incomplete = _search_collection(d)
        out["artists"] = [{"id": str(u.get("id")), "name": u.get("username") or "?",
                           "url": u.get("permalink_url") or ""}
                          for u in collection]
        if incomplete:
            out["errors"].append(provider_collection_incomplete("soundcloud").detail)
    except Exception as exc:
        out["errors"].append(detail_from_exception("soundcloud", exc))
    return out


def _search_collection(data: object) -> tuple[list[dict], bool]:
    if not isinstance(data, dict) or not isinstance(data.get("collection"), list):
        raise provider_collection_incomplete("soundcloud")
    raw = data["collection"]
    valid = [item for item in raw if isinstance(item, dict) and item.get("id") is not None]
    return valid, len(valid) != len(raw)


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
    if not data.get("errors"):
        _cache[url] = (time.time(), data)
    return data


def _resolve_api(url: str, token: str | None) -> dict:
    """api-v2 /resolve: сет (1 запрос, треки с метаданными) или юзер (+треки юзера)."""
    obj = _api_get("/resolve", token, url=url)
    kind = obj.get("kind")
    if kind == "playlist":
        raw_tracks = obj.get("tracks")
        if not isinstance(raw_tracks, list):
            raise provider_collection_incomplete("soundcloud")
        tracks, errors = [], []
        for item in raw_tracks:
            if not isinstance(item, dict) or item.get("id") is None:
                errors.append(provider_collection_incomplete("soundcloud").detail)
                continue
            tracks.append(_track_from_api(item))
        if 'track_count' in obj and (type(obj['track_count']) is not int or obj['track_count'] != len(tracks)):
            errors.append(provider_collection_incomplete('soundcloud').detail)
        return {"id": str(obj["id"]), "title": obj.get("title") or "playlist",
                "tracks": tracks, "errors": errors,
                **({'track_count': obj['track_count']} if 'track_count' in obj else {})}
    if kind == "user":
        tracks, errors = [], []
        first_url = f"{SC_API}/users/{obj['id']}/tracks"
        try:
            for data in _account_pages(first_url, token or ""):
                for item in data["collection"]:
                    if not isinstance(item, dict) or item.get("id") is None:
                        errors.append(provider_collection_incomplete("soundcloud").detail)
                        continue
                    tracks.append(_track_from_api(item))
        except Exception as exc:
            errors.append(detail_from_exception("soundcloud", exc))
        return {"id": str(obj["id"]), "title": obj.get("username") or "user",
                "tracks": tracks, "errors": errors}
    if kind == "track":
        return {"id": str(obj["id"]), "title": obj.get("title") or "track",
                "tracks": [_track_from_api(obj)], "errors": []}
    raise provider_collection_incomplete("soundcloud")


def _resolve_likes(url: str) -> dict:
    """Лайки — только yt-dlp с oauth-cookie (публичный API их закрыл).
    Затем добиваем artist/duration батчами через api-v2 /tracks?ids=."""
    token = sc_oauth_token()
    if not token:
        raise RuntimeError("лайки доступны после входа (SC: вход)")
    opts = _youtube_dl_opts_with_oauth(
        {"quiet": True, "ignoreerrors": True, "extract_flat": True},
        token,
    )
    with yt_dlp.YoutubeDL(opts) as y:
        _bind_cookiejar(y, opts)
        info = y.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp не смог прочитать лайки")
    tracks, errors = [], []
    entries = info.get('entries')
    if entries is None or isinstance(entries, (str, dict)) or not hasattr(entries, '__iter__'):
        raise provider_collection_incomplete('soundcloud')
    for e in entries:
        if not isinstance(e, dict) or e.get("id") is None:
            errors.append(provider_collection_incomplete("soundcloud").detail)
            continue
        tracks.append({
            "id": str(e.get("id")),
            "title": e.get("title") or "?",
            "artist": e.get("uploader") or "",
            "album": "",
            "duration": int(e.get("duration") or 0),
            "url": e.get("url") or e.get("webpage_url") or "",
        })
    if 'playlist_count' in info and (type(info['playlist_count']) is not int or info['playlist_count'] != len(tracks)):
        errors.append(provider_collection_incomplete('soundcloud').detail)
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
        except Exception as exc:
            errors.append(detail_from_exception("soundcloud", exc))
        time.sleep(0.4)  # пейсинг против рейт-лимита
    return {"id": "likes", "title": info.get("title") or "❤ Лайки",
            "tracks": tracks, "errors": errors,
            **({'playlist_count': info['playlist_count']} if 'playlist_count' in info else {})}


def download_track(track: dict, out_dir: Path):
    """Скачивает трек по url. Возвращает (path, format, actual_duration)."""
    check_cancelled()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(f"{track['artist']} - {track['title']}" if track.get("artist")
                             else track["title"])
    staged_template = make_staged_path(out_dir / (base + ".download")).with_suffix(".%(ext)s")
    stage_prefix = staged_template.name.split("%(ext)s", 1)[0]
    register_stage(out_dir / stage_prefix, prefix=True)
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        # предпочитаем progressive MP3 (точная длительность, чистый контейнер),
        # затем AAC 160k (HLS — возможен сдвиг длительности на неск. секунд)
        "format": "http_mp3_1_0/hls_mp3_1_0/hls_aac_160k/bestaudio/best",
        "outtmpl": str(staged_template),
        "ffmpeg_location": FFMPEG,
        "postprocessor_args": ["-movflags", "+faststart"],
        "progress_hooks": [lambda _progress: check_cancelled()],
        "postprocessor_hooks": [lambda _progress: check_cancelled()],
        "socket_timeout": 10,
    }
    opts = _youtube_dl_opts_with_oauth(opts, sc_oauth_token())
    try:
        check_cancelled()
        with cancellable_ytdlp_processes(), yt_dlp.YoutubeDL(opts) as y:
            _bind_cookiejar(y, opts)
            info = y.extract_info(track["url"], download=True)
            check_cancelled()
            fpath = Path(y.prepare_filename(info))
    except Exception:
        cleanup_owned_stages(*_current_stage_candidates(out_dir, stage_prefix))
        check_cancelled()
        raise RuntimeError("SoundCloud download failed") from None
    # yt-dlp может поменять расширение после пост-обработки
    if not fpath.exists() or not _is_current_stage_path(fpath, out_dir, stage_prefix):
        cands = _current_stage_candidates(out_dir, stage_prefix)
        if not cands:
            cleanup_owned_stages(*_current_stage_candidates(out_dir, stage_prefix))
            raise RuntimeError("SoundCloud download failed")
        fpath = cands[0]
    return fpath, fpath.suffix.lstrip(".").lower(), float(info.get("duration") or 0), info
