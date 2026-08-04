# -*- coding: utf-8 -*-
"""DeckPipe MVP — FastAPI бэкенд."""
import asyncio
import time
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, library
from .deezer_client import load_config, get_session

app = FastAPI(title="DeckPipe")
STATIC = Path(__file__).parent / "static"
APP_VERSION = "0.5.0"


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}

# ---------- кеши ----------
_cache = {"playlists": (0, None), "tracks": {}}
PLAYLISTS_TTL = 120
TRACKS_TTL = 60


# ---------- Deezer данные ----------
async def fetch_playlists():
    ts, data = _cache["playlists"]
    if data is not None and time.time() - ts < PLAYLISTS_TTL:
        return data
    from deezer_python_gql import DeezerGQLClient
    arl = load_config().get("arl")
    client = DeezerGQLClient(arl=arl)
    res = await client.get_user_playlists()
    out = []
    for e in res.playlists.edges:
        n = e.node
        out.append({
            "id": n.id, "title": n.title,
            "count": n.estimated_tracks_count,
            "cover": (n.picture.urls[0] if n.picture and n.picture.urls else None),
        })
    _cache["playlists"] = (time.time(), out)
    return out


def fetch_tracks(playlist_id: str):
    ts, data = _cache["tracks"].get(playlist_id, (0, None))
    if data is not None and time.time() - ts < TRACKS_TTL:
        return data
    # публичный API (быстро, но только для public-плейлистов)
    try:
        tracks = _fetch_tracks_public(playlist_id)
    except Exception:
        # приватный плейлист — через GraphQL с ARL
        tracks = asyncio.run(_fetch_tracks_graphql(playlist_id))
    _cache["tracks"][playlist_id] = (time.time(), tracks)
    return tracks


def _fetch_tracks_public(playlist_id: str) -> list:
    tracks, index = [], 0
    while True:
        r = requests.get(f"https://api.deezer.com/playlist/{playlist_id}/tracks",
                         params={"limit": 100, "index": index}, timeout=30)
        d = r.json()
        if "data" not in d:
            raise HTTPException(502, f"deezer api: {d.get('error', d)}")
        for t in d["data"]:
            if not isinstance(t, dict) or "id" not in t:
                continue
            tracks.append({
                "id": str(t["id"]), "title": t.get("title") or "?",
                "artist": (t.get("artist") or {}).get("name", ""),
                "album": (t.get("album") or {}).get("title", ""),
                "duration": t.get("duration") or 0,
            })
        if "next" not in d or not d["data"]:
            break
        index += 100
    return tracks


async def _fetch_tracks_graphql(playlist_id: str) -> list:
    from deezer_python_gql import DeezerGQLClient
    client = DeezerGQLClient(arl=load_config().get("arl"))
    tracks, after = [], None
    while True:
        pl = await client.get_playlist(playlist_id=playlist_id,
                                       tracks_first=100, tracks_after=after)
        conn = pl.tracks
        for e in conn.edges:
            n = e.node
            artists = [c.node.name for c in n.contributors.edges
                       if c.node and "MAIN" in (c.roles or [])]
            tracks.append({
                "id": str(n.id), "title": n.title or "?",
                "artist": ", ".join(artists) or "",
                "album": (n.album.display_title if n.album else "") or "",
                "duration": n.duration or 0,
            })
        page = getattr(conn, "page_info", None) or getattr(conn, "pageInfo", None)
        if not page or not page.has_next_page:
            break
        after = page.end_cursor
    return tracks


# ---------- API ----------
class ConfigIn(BaseModel):
    music_root: str | None = None
    wav_mode: str | None = None  # source|wav|wav_delete
    numbering: bool | None = None


class DownloadIn(BaseModel):
    tracks: list


class BindIn(BaseModel):
    path: str


@app.get("/api/config")
def api_config():
    cfg = load_config()
    user = None
    try:
        ds = get_session()
        user = {"id": ds.user["USER_ID"], "email": ds.user.get("EMAIL")}
    except Exception as e:
        user = {"error": str(e)}
    return {"music_root": str(library.music_root()), "arl_set": bool(cfg.get("arl")),
            "wav_mode": cfg.get("wav_mode", "source"),
            "numbering": cfg.get("numbering", True),
            "sc_user": cfg.get("sc_username") or None,
            "user": user}


@app.post("/api/config")
def api_set_config(c: ConfigIn):
    if c.music_root:
        p = Path(c.music_root)
        p.mkdir(parents=True, exist_ok=True)
        library.set_music_root(str(p))
    if c.wav_mode in ("source", "wav", "wav_delete"):
        cfg = load_config()
        cfg["wav_mode"] = c.wav_mode
        save_config(cfg)
    if c.numbering is not None:
        cfg = load_config()
        cfg["numbering"] = bool(c.numbering)
        save_config(cfg)
    return api_config()


@app.get("/api/playlists")
async def api_playlists():
    pls = await fetch_playlists()
    for p in pls:
        pl_dir = library.playlist_dir(p["id"], p["title"])
        sc = library.load_sidecar(pl_dir)
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif e.get("status") == "ok" and (pl_dir / e.get("file", "")).exists():
                ok += 1
        p.update({"ok": ok, "errors": err, "path": str(pl_dir)})
    return pls


@app.get("/api/playlists/{playlist_id}/tracks")
def api_tracks(playlist_id: str, title: str = ""):
    deezer_tracks = fetch_tracks(playlist_id)
    pl_dir = library.playlist_dir(playlist_id, title)
    return {"path": str(pl_dir), "tracks": library.scan_playlist(pl_dir, deezer_tracks)}


@app.post("/api/playlists/{playlist_id}/download")
def api_download(playlist_id: str, body: DownloadIn, title: str = "", mode: str = "append"):
    if not body.tracks:
        raise HTTPException(400, "пустой список треков")
    job_id = jobs.enqueue(playlist_id, title or playlist_id, body.tracks, mode=mode)
    return {"job_id": job_id}


class RenumberIn(BaseModel):
    order: list  # [track_id, ...] в нужном порядке
    total: int = 0


@app.post("/api/playlists/{key}/renumber")
def api_renumber(key: str, body: RenumberIn, title: str = ""):
    pl_dir = library.playlist_dir(key, title or key)
    digits = library.digits_for(body.total or len(body.order))
    renamed = library.renumber_playlist(pl_dir, [str(i) for i in body.order], digits)
    return {"renamed": renamed}


@app.post("/api/playlists/{playlist_id}/bind")
def api_bind(playlist_id: str, body: BindIn):
    p = Path(body.path)
    p.mkdir(parents=True, exist_ok=True)
    library.bind_playlist(playlist_id, str(p))
    return {"path": str(p)}


@app.get("/api/browse")
def api_browse():
    """Диалог выбора папки. PowerShell FolderBrowserDialog (надёжно на Windows),
    fallback — tkinter. Возвращает путь или null."""
    import subprocess
    ps_cmd = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = 'Выберите папку плейлиста'; $d.ShowNewFolderButton = $true; "
        "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
    )
    ps_exe = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
    try:
        r = subprocess.run(
            [ps_exe, "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=300)
        path = r.stdout.decode("utf-8", errors="replace").strip()
        if r.returncode == 0:
            return {"path": path or None}
    except Exception:
        pass
    # fallback: tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Выберите папку плейлиста", parent=root)
        root.destroy()
        return {"path": path or None}
    except Exception:
        return {"path": None}


@app.get("/api/jobs")
def api_jobs():
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, "job не найден")
    return j


# ---------- SoundCloud ----------
from . import soundcloud
from .deezer_client import save_config


def _sc_sources() -> list:
    return load_config().get("sc_sources", [])


def _sc_save_sources(sources: list):
    cfg = load_config()
    cfg["sc_sources"] = sources
    save_config(cfg)


def _sc_key(source_id: str) -> str:
    return f"sc:{source_id}"


class ScSourceIn(BaseModel):
    url: str


@app.get("/api/sc/sources")
def api_sc_sources():
    out = []
    for s in _sc_sources():
        pl_dir = library.playlist_dir(_sc_key(s["id"]), s["title"])
        sc = library.load_sidecar(pl_dir)
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif e.get("status") == "ok" and (pl_dir / e.get("file", "")).exists():
                ok += 1
        out.append({**s, "ok": ok, "errors": err, "path": str(pl_dir)})
    return out


@app.post("/api/sc/sources")
def api_sc_add(body: ScSourceIn):
    try:
        data = soundcloud.resolve(body.url)
    except Exception as e:
        raise HTTPException(400, f"не удалось разобрать URL: {e}")
    sources = _sc_sources()
    if any(s["id"] == data["id"] for s in sources):
        raise HTTPException(409, "этот источник уже добавлен")
    src = {"id": data["id"], "url": body.url, "title": data["title"],
           "count": len(data["tracks"])}
    sources.append(src)
    _sc_save_sources(sources)
    return src


@app.delete("/api/sc/sources/{source_id}")
def api_sc_delete(source_id: str):
    sources = _sc_sources()
    _sc_save_sources([s for s in sources if s["id"] != source_id])
    return {"ok": True}


@app.get("/api/sc/sources/{source_id}/tracks")
def api_sc_tracks(source_id: str):
    src = next((s for s in _sc_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(404, "источник не найден")
    try:
        data = soundcloud.resolve(src["url"], use_cache=False)
    except Exception as e:
        raise HTTPException(502, f"SoundCloud: {str(e)[:300]}")
    pl_dir = library.playlist_dir(_sc_key(source_id), src["title"])
    return {"path": str(pl_dir), "tracks": library.scan_playlist(pl_dir, data["tracks"])}


@app.post("/api/sc/sources/{source_id}/download")
def api_sc_download(source_id: str, body: DownloadIn, mode: str = "append"):
    src = next((s for s in _sc_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(404, "источник не найден")
    tracks = [{**t, "provider": t.get("provider") or "sc"} for t in body.tracks]
    job_id = jobs.enqueue(_sc_key(source_id), src["title"], tracks, mode=mode)
    return {"job_id": job_id}


# ---------- Локальные плейлисты (для SoundCloud-целей из поиска) ----------
# SoundCloud API не позволяет сторонним приложениям создавать плейлисты
# (регистрация приложений закрыта, write-endpoints недоступны) — поэтому
# цель для SC-треков создаётся локально; URL можно привязать позже.

def _local_sources() -> list:
    return load_config().get("local_sources", [])


def _local_save(sources: list):
    cfg = load_config()
    cfg["local_sources"] = sources
    save_config(cfg)


class LocalPlaylistIn(BaseModel):
    title: str
    url: str | None = None  # опционально: связать со страницей SC позже


@app.get("/api/local/playlists")
def api_local_playlists():
    out = []
    for s in _local_sources():
        key = f"local:{s['id']}"
        pl_dir = library.playlist_dir(key, s["title"])
        sc = library.load_sidecar(pl_dir)
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif e.get("status") == "ok" and (pl_dir / e.get("file", "")).exists():
                ok += 1
        out.append({**s, "key": key, "ok": ok, "errors": err, "path": str(pl_dir)})
    return out


@app.post("/api/local/playlists")
def api_local_create(body: LocalPlaylistIn):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "пустое название")
    src = {"id": uuid.uuid4().hex[:8], "title": title,
           "url": (body.url or "").strip(), "count": 0, "local": True}
    sources = _local_sources()
    sources.append(src)
    _local_save(sources)
    return {"ok": True, "key": f"local:{src['id']}", **src}


# ---------- Логин ----------
class LoginDeezerIn(BaseModel):
    arl: str


class LoginScIn(BaseModel):
    oauth_token: str


@app.post("/api/login/deezer")
def api_login_deezer(body: LoginDeezerIn):
    from .deezer_client import DeezerSession, save_config, _session_cache
    try:
        ds = DeezerSession(body.arl.strip())
    except Exception as e:
        raise HTTPException(401, f"ARL не принят: {e}")
    cfg = load_config()
    cfg["arl"] = body.arl.strip()
    save_config(cfg)
    _session_cache["session"] = None  # сброс кеша сессии
    return {"id": ds.user["USER_ID"], "email": ds.user.get("EMAIL")}


@app.post("/api/login/soundcloud")
def api_login_sc(body: LoginScIn):
    token = body.oauth_token.strip()
    try:
        user = soundcloud.sc_validate(token)
    except Exception as e:
        raise HTTPException(401, f"oauth_token не принят: {e}")
    cfg = load_config()
    cfg["sc_oauth"] = token
    cfg["sc_username"] = user.get("username", "")
    save_config(cfg)
    return {"id": user.get("id"), "username": user.get("username")}


@app.get("/api/sc/account")
def api_sc_account():
    token = soundcloud.sc_oauth_token()
    if not token:
        raise HTTPException(401, "SoundCloud: не выполнен вход")
    playlists = soundcloud.sc_account_playlists(token)
    return {"playlists": playlists,
            "likes": {"id": "likes", "title": "❤ Лайки", "url": "https://soundcloud.com/you/likes"}}


class ScImportIn(BaseModel):
    items: list  # [{id,title,url}]


@app.post("/api/sc/account/import")
def api_sc_import(body: ScImportIn):
    sources = _sc_sources()
    added = 0
    for it in body.items:
        if not it.get("url") or any(s["url"] == it["url"] for s in sources):
            continue
        sources.append({"id": str(it["id"]), "url": it["url"],
                        "title": it.get("title", "?"), "count": it.get("count", 0)})
        added += 1
    _sc_save_sources(sources)
    return {"added": added}


@app.post("/api/sc/sync-account")
def api_sc_sync_account(force: bool = False):
    """Автосинк: подтягивает свои + лайкнутые плейлисты и лайки в источники.
    Не чаще раза в 5 минут (рейт-лимит SC), force — принудительно."""
    token = soundcloud.sc_oauth_token()
    if not token:
        raise HTTPException(401, "SoundCloud: не выполнен вход")
    cfg = load_config()
    if not force and time.time() - cfg.get("sc_last_sync", 0) < 300:
        return {"added": 0, "total": len(_sc_sources()), "skipped": True}
    try:
        me = soundcloud.sc_validate(token)
        account = soundcloud.sc_account_playlists(token)
    except Exception as e:
        raise HTTPException(503, f"SoundCloud недоступен: {e}")
    likes_url = f"https://soundcloud.com/{me.get('permalink', 'you')}/likes"
    items = [{"id": "likes", "title": "❤ Лайки", "url": likes_url, "count": me.get("likes_count", 0)}]
    items += account
    sources = _sc_sources()
    # вычищаем устаревший источник лайков (you/likes)
    sources = [s for s in sources if not s["url"].endswith("/you/likes")]
    added = 0
    by_url = {s["url"]: s for s in sources}
    for it in items:
        if not it.get("url"):
            continue
        existing = by_url.get(it["url"])
        if existing:  # обновляем название/счётчик (чинит старые битые данные)
            existing["title"] = it.get("title", existing["title"])
            existing["count"] = it.get("count", existing.get("count", 0))
            continue
        sources.append({"id": str(it["id"]), "url": it["url"],
                        "title": it.get("title", "?"), "count": it.get("count", 0)})
        added += 1
    cfg["sc_sources"] = sources
    cfg["sc_last_sync"] = time.time()
    save_config(cfg)  # одна запись: и sources, и last_sync
    return {"added": added, "total": len(sources)}


# ---------- Поиск (Deezer + SoundCloud) ----------
def _search_deezer(q: str) -> dict:
    out = {"tracks": [], "albums": [], "artists": []}
    try:
        d = requests.get("https://api.deezer.com/search", params={"q": q, "limit": 10}, timeout=20).json()
        out["tracks"] = [{"id": str(t["id"]), "title": t.get("title") or "?",
                          "artist": (t.get("artist") or {}).get("name", ""),
                          "album": (t.get("album") or {}).get("title", ""),
                          "duration": t.get("duration") or 0,
                          "url": t.get("link") or "", "provider": "deezer"}
                         for t in d.get("data", [])]
    except Exception:
        pass
    try:
        d = requests.get("https://api.deezer.com/search/album", params={"q": q, "limit": 5}, timeout=20).json()
        out["albums"] = [{"id": str(a["id"]), "title": a.get("title") or "?",
                          "artist": (a.get("artist") or {}).get("name", ""),
                          "url": a.get("link") or ""} for a in d.get("data", [])]
    except Exception:
        pass
    try:
        d = requests.get("https://api.deezer.com/search/artist", params={"q": q, "limit": 5}, timeout=20).json()
        out["artists"] = [{"id": str(a["id"]), "name": a.get("name") or "?",
                           "url": a.get("link") or ""} for a in d.get("data", [])]
    except Exception:
        pass
    return out


@app.get("/api/search")
def api_search(q: str, service: str = "both"):
    out = {}
    if service in ("both", "deezer"):
        out["deezer"] = _search_deezer(q)
    if service in ("both", "sc"):
        sc_res = soundcloud.search(q)
        for t in sc_res["tracks"]:
            t["provider"] = "sc"
        out["sc"] = sc_res
    return out


class SearchDownloadIn(BaseModel):
    target_key: str       # deezer playlist id | sc:<id> | local:<id> | dir:<key>
    target_title: str
    tracks: list          # [{id,title,artist,duration,url?,provider}]
    target_dir: str | None = None  # произвольная папка скачивания


def _is_deezer_playlist_key(key: str) -> bool:
    """Плейлист Deezer — числовой id без префиксов sc:/local:/dir:."""
    return not key.startswith(("sc:", "local:", "dir:"))


@app.post("/api/search/download")
async def api_search_download(body: SearchDownloadIn):
    """Скачать найденные треки в выбранный плейлист/папку.
    Если цель — плейлист Deezer, треки также добавляются в него на сервисе."""
    if not body.tracks:
        raise HTTPException(400, "пустой список треков")
    target_key, target_title = body.target_key, body.target_title
    if body.target_dir:
        # произвольная папка: разовый биндинг dir:<hash> -> путь, sidecar живёт в ней
        p = Path(body.target_dir)
        if not p.is_absolute():
            raise HTTPException(400, "target_dir должен быть абсолютным путём")
        p.mkdir(parents=True, exist_ok=True)
        target_key = "dir:" + uuid.uuid4().hex[:8]
        library.bind_playlist(target_key, str(p))
        target_title = target_title or p.name
    job_id = jobs.enqueue(target_key, target_title, body.tracks, mode="append")
    added_remote = 0
    if not body.target_dir and _is_deezer_playlist_key(body.target_key):
        dz_ids = [str(t["id"]) for t in body.tracks if t.get("provider", "deezer") == "deezer"]
        if dz_ids:
            try:
                from deezer_python_gql import DeezerGQLClient
                client = DeezerGQLClient(arl=load_config().get("arl"))
                await client.add_tracks_to_playlist(playlist_id=body.target_key, track_ids=dz_ids)
                added_remote = len(dz_ids)
                _cache["tracks"].pop(body.target_key, None)
            except Exception:
                pass  # не критично: локальная загрузка идёт независимо
    return {"job_id": job_id, "added_to_deezer": added_remote}


class DzAddIn(BaseModel):
    playlist_id: str
    track_ids: list


class DzCreateIn(BaseModel):
    title: str
    track_ids: list = []


@app.post("/api/deezer/playlist/add")
async def api_dz_add(body: DzAddIn):
    """Добавить треки в плейлист Deezer без скачивания."""
    try:
        from deezer_python_gql import DeezerGQLClient
        client = DeezerGQLClient(arl=load_config().get("arl"))
        await client.add_tracks_to_playlist(playlist_id=body.playlist_id,
                                            track_ids=[str(i) for i in body.track_ids])
        _cache["playlists"] = (0, None)
        _cache["tracks"].pop(body.playlist_id, None)
        return {"ok": True, "added": len(body.track_ids)}
    except Exception as e:
        raise HTTPException(502, f"deezer: {e}")


@app.post("/api/deezer/playlist/create")
async def api_dz_create(body: DzCreateIn):
    """Создать плейлист в Deezer (+ опционально сразу добавить треки)."""
    try:
        from deezer_python_gql import DeezerGQLClient
        client = DeezerGQLClient(arl=load_config().get("arl"))
        pl = await client.create_playlist(title=body.title, is_private=False, is_collaborative=False)
        inner = getattr(pl, "playlist", pl)
        pid = str(getattr(inner, "id"))
        if body.track_ids:
            await client.add_tracks_to_playlist(playlist_id=pid,
                                                track_ids=[str(i) for i in body.track_ids])
        _cache["playlists"] = (0, None)
        return {"ok": True, "id": pid, "title": body.title}
    except Exception as e:
        raise HTTPException(502, f"deezer: {e}")


@app.get("/api/deezer/album/{album_id}")
def api_dz_album(album_id: str):
    """Треки альбома Deezer для скачивания из поиска."""
    d = requests.get(f"https://api.deezer.com/album/{album_id}", timeout=20).json()
    if "tracks" not in d:
        raise HTTPException(502, f"deezer: {d.get('error', 'album not found')}")
    return [{"id": str(t["id"]), "title": t.get("title") or "?",
             "artist": (t.get("artist") or {}).get("name", ""),
             "album": d.get("title", ""),
             "duration": t.get("duration") or 0, "provider": "deezer"}
            for t in d["tracks"].get("data", [])]


@app.get("/api/sc/resolve-tracks")
def api_sc_resolve_tracks(url: str):
    """Треки сета/страницы SC для скачивания из поиска."""
    try:
        data = soundcloud.resolve(url)
    except Exception as e:
        raise HTTPException(502, f"SoundCloud: {e}")
    return [{**t, "provider": "sc"} for t in data["tracks"]]


# ---------- Rekordbox ----------
from . import rekordbox as rb


@app.get("/api/rb/status")
def api_rb_status():
    out = {"db_exists": rb.db_exists(), "running": rb.rb_running(), "playlists": []}
    if out["db_exists"] and not out["running"]:
        try:
            out["playlists"] = rb.get_rb_playlists()
        except Exception as e:
            out["error"] = str(e)
    return out


class RbSyncIn(BaseModel):
    playlist_key: str
    playlist_title: str


@app.post("/api/rb/sync")
def api_rb_sync(body: RbSyncIn):
    """Синк локального плейлиста (ок-треки по порядку) в Rekordbox."""
    pl_dir = library.playlist_dir(body.playlist_key, body.playlist_title)
    sc = library.load_sidecar(pl_dir)
    entries = [e for e in sc.get("tracks", {}).values()
               if e.get("status") == "ok" and e.get("file") and (pl_dir / e["file"]).exists()]
    entries.sort(key=lambda e: (int(e.get("position") or 10 ** 6), e["file"].lower()))
    if not entries:
        raise HTTPException(400, "в плейлисте нет скачанных треков (✔)")
    files = [pl_dir / e["file"] for e in entries]
    try:
        return rb.sync_playlist(body.playlist_title, files)
    except Exception as e:
        raise HTTPException(409, str(e))


class FlipIn(BaseModel):
    playlist_key: str
    playlist_title: str
    to_wav: bool
    workers: int = 0


@app.post("/api/flip")
def api_flip(body: FlipIn):
    """WAV-flip: конвертация + перенос путей в master.db (все кью/сетка сохраняются)."""
    job_id = jobs.enqueue_flip(body.playlist_key, body.playlist_title,
                               body.to_wav, body.workers)
    return {"job_id": job_id}


@app.get("/api/errors")
async def api_errors():
    """Все треки со статусом verify_failed_* по всем плейлистам и источникам."""
    out = []
    try:
        playlists = await fetch_playlists()
    except Exception:
        playlists = []
    entries = [(p["id"], p["title"], "deezer") for p in playlists]
    entries += [(_sc_key(s["id"]), s["title"], "sc") for s in _sc_sources()]

    for key, title, provider in entries:
        pl_dir = library.playlist_dir(key, title)
        sc = library.load_sidecar(pl_dir)
        for tid, e in sc.get("tracks", {}).items():
            if str(e.get("status", "")).startswith("verify_failed"):
                out.append({
                    "playlist_key": key, "playlist_title": title, "provider": provider,
                    "error": e.get("error", ""), "file": e.get("file", ""),
                    "status": e["status"],
                    "track": {"id": tid, "title": e.get("title", "?"),
                              "artist": e.get("artist", ""),
                              "duration": e.get("duration_expected", 0),
                              "url": e.get("url", ""),
                              "position": e.get("position")},
                })
    return out


class RetryIn(BaseModel):
    playlist_key: str
    playlist_title: str
    track: dict


class ReportIn(BaseModel):
    text: str
    current: str | None = None


@app.post("/api/report")
def api_report(body: ReportIn):
    from .bugreport import send_report
    errs = []
    for j in jobs.list_jobs()[:3]:
        for r in j.get("results", []):
            if not r.get("ok"):
                errs.append(f"{r.get('title')}: {str(r.get('error'))[:100]}")
    res = send_report(body.text, context={"jobs_errors": errs, "current": body.current})
    if not res["ok"]:
        raise HTTPException(502, res["error"])
    return res


@app.post("/api/errors/retry")
def api_retry(body: RetryIn):
    provider = "sc" if body.playlist_key.startswith("sc:") else "deezer"
    track = {**body.track, "provider": provider}
    # если у трека была позиция — восстанавливаем её, а не кидаем вниз
    mode = "playlist_order" if track.get("position") else "append"
    job_id = jobs.enqueue(body.playlist_key, body.playlist_title, [track], mode=mode)
    return {"job_id": job_id}


# ---------- статика ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
