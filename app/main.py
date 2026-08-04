# -*- coding: utf-8 -*-
"""DeckPipe MVP — FastAPI бэкенд."""
import asyncio
import time
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
    # публичный API (плейлисты юзера публичные); приватные — TODO через GraphQL
    tracks, index = [], 0
    while True:
        r = requests.get(f"https://api.deezer.com/playlist/{playlist_id}/tracks",
                         params={"limit": 100, "index": index}, timeout=30)
        d = r.json()
        if "data" not in d:
            raise HTTPException(502, f"deezer api: {d.get('error', d)}")
        for t in d["data"]:
            tracks.append({
                "id": str(t["id"]), "title": t["title"],
                "artist": t["artist"]["name"],
                "album": t.get("album", {}).get("title", ""),
                "duration": t.get("duration", 0),
            })
        if "next" not in d or not d["data"]:
            break
        index += 100
    _cache["tracks"][playlist_id] = (time.time(), tracks)
    return tracks


# ---------- API ----------
class ConfigIn(BaseModel):
    music_root: str | None = None


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
            "user": user}


@app.post("/api/config")
def api_set_config(c: ConfigIn):
    if c.music_root:
        p = Path(c.music_root)
        p.mkdir(parents=True, exist_ok=True)
        library.set_music_root(str(p))
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
def api_download(playlist_id: str, body: DownloadIn, title: str = ""):
    if not body.tracks:
        raise HTTPException(400, "пустой список треков")
    job_id = jobs.enqueue(playlist_id, title or playlist_id, body.tracks)
    return {"job_id": job_id}


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
    data = soundcloud.resolve(src["url"])
    pl_dir = library.playlist_dir(_sc_key(source_id), src["title"])
    return {"path": str(pl_dir), "tracks": library.scan_playlist(pl_dir, data["tracks"])}


@app.post("/api/sc/sources/{source_id}/download")
def api_sc_download(source_id: str, body: DownloadIn):
    src = next((s for s in _sc_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(404, "источник не найден")
    tracks = [{**t, "provider": "sc"} for t in body.tracks]
    job_id = jobs.enqueue(_sc_key(source_id), src["title"], tracks)
    return {"job_id": job_id}


# ---------- Лист ошибок ----------
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
                              "url": e.get("url", "")},
                })
    return out


class RetryIn(BaseModel):
    playlist_key: str
    playlist_title: str
    track: dict


@app.post("/api/errors/retry")
def api_retry(body: RetryIn):
    provider = "sc" if body.playlist_key.startswith("sc:") else "deezer"
    track = {**body.track, "provider": provider}
    job_id = jobs.enqueue(body.playlist_key, body.playlist_title, [track])
    return {"job_id": job_id}


# ---------- статика ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
