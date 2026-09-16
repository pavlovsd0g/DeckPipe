# -*- coding: utf-8 -*-
"""DeckPipe MVP — FastAPI бэкенд."""
import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, library, catalog_service, activity_log
from .library_catalog import MusicRootRequired, configured_music_roots
from .auth_broker import auth_router, register_invalidation_hook, recover_pending_transaction
from .deezer_client import (
    CONFIG_PATH,
    ROOT,
    SECRET_STORE_PATH,
    get_deezer_arl,
    get_session,
    load_config,
    set_deezer_arl,
    set_soundcloud_oauth,
)
from .security import LoopbackSecurityMiddleware, SecuritySettings
from .provider_errors import (
    detail_from_exception,
    provider_collection_incomplete,
    provider_error,
    provider_exception,
    provider_pagination_invalid,
)
from .remote_actions import RemoteActionStore


async def run_startup_migrations() -> None:
    from .secure_store import migrate_legacy_config

    recover_pending_transaction()
    migrate_legacy_config(config_path=CONFIG_PATH, store_path=SECRET_STORE_PATH)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    activity_log.initialize(ROOT)
    activity_log.record('info', 'application', 'startup', 'Запуск DeckPipe.')
    await run_startup_migrations()
    jobs.initialize(ROOT)
    yield
    activity_log.record('info', 'application', 'shutdown', 'Завершение работы DeckPipe.')


app = FastAPI(title="DeckPipe", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(activity_log.ActivityLogMiddleware)
app.add_middleware(LoopbackSecurityMiddleware, settings=SecuritySettings.from_env())
app.include_router(auth_router)
app.include_router(activity_log.router)
STATIC = Path(__file__).parent / "static"


@app.exception_handler(MusicRootRequired)
@app.exception_handler(catalog_service.LibraryUnavailable)
async def library_error(_request, exc):
    return JSONResponse(status_code=409, content={"detail": {"code": exc.code, "message": str(exc)}})


def _require_music_root():
    return configured_music_roots(load_config())[0]


def _enqueue_missing(key: str, title: str, tracks: list, mode: str) -> dict:
    _require_music_root()
    if mode not in {"append", "playlist_order"}:
        raise HTTPException(400, "Неизвестный режим синхронизации.")
    try:
        if key.startswith('local:'):
            from .rekordbox_service import persist_local_membership, SourceError
            source = next((s for s in _local_sources() if str(s['id']) == key[6:]), None)
            if not source:
                raise HTTPException(404, {'code': 'source_unknown'})
            title = source['title']
            try:
                directory = catalog_service.validate_destination(library.playlist_dir(key, title), require_online=True)
                tracks = persist_local_membership(directory, tracks, mode=mode)
            except SourceError as exc:
                raise HTTPException(409, {'code': exc.code}) from None
        prepared = catalog_service.prepare_download(library.playlist_dir(key, title), tracks)
    except MusicRootRequired:
        raise
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Проверьте выбранные треки и папку плейлиста.") from None
    job_id = jobs.enqueue(key, title, prepared['tracks'], mode=mode) if prepared['tracks'] else None
    return {"job_id": job_id, "already_present": prepared['already_present'], "needs_attention": prepared['needs_attention']}


def _require_legacy_login_mode():
    if os.environ.get('DECKPIPE_AUTH_BROKER_TOKEN'):
        raise HTTPException(403, {"code": "native_auth_required", "message": "Используйте вход через браузер в приложении Windows."})


class ConfirmLocationIn(BaseModel):
    track: dict
    path: str


@app.get("/api/library/status")
def api_library_status():
    return catalog_service.scan_status()


@app.post("/api/library/scan")
def api_library_scan():
    return catalog_service.begin_scan()


@app.post("/api/library/confirm")
def api_library_confirm(body: ConfirmLocationIn):
    _require_music_root()
    try:
        return catalog_service.confirm_location(body.track, body.path)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Выберите доступный файл из подключённой музыкальной библиотеки.") from None


APP_VERSION = "0.6.0"
APP_BUILD_ID = "0.6.0+20260827.050713.6456dba254a6"


def _public_api_error(service: str) -> str:
    return f"{service}: operation failed"


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION, "build_id": APP_BUILD_ID}

# ---------- кеши ----------
_cache = {"playlists": (0, None), "tracks": {}}
PLAYLISTS_TTL = 120
TRACKS_TTL = 60
PROVIDER_PAGE_LIMIT = 1000
_remote_action_store = RemoteActionStore(ROOT / "remote-actions.json")


def _invalidate_provider_data(provider):
    if provider == 'deezer':
        _cache['playlists'] = (0, None)
        _cache['tracks'].clear()


register_invalidation_hook(_invalidate_provider_data)


# ---------- Deezer данные ----------
async def fetch_playlists():
    ts, data = _cache["playlists"]
    if data is not None and time.time() - ts < PLAYLISTS_TTL:
        return data
    client = _deezer_gql_client()
    out, after, seen = [], None, set()
    for _page_number in range(PROVIDER_PAGE_LIMIT):
        try:
            res = await client.get_user_playlists(first=50, after=after)
            conn = res.playlists
            edges = conn.edges
        except Exception as exc:
            raise provider_exception("deezer", exc) from None
        if edges is None:
            raise provider_collection_incomplete("deezer")
        for e in edges:
            n = getattr(e, "node", None)
            if n is None or getattr(n, "id", None) is None:
                raise provider_collection_incomplete("deezer")
            out.append({
                "id": n.id, "title": n.title,
                "count": n.estimated_tracks_count,
                "cover": (n.picture.urls[0] if n.picture and n.picture.urls else None),
            })
        page = getattr(conn, "page_info", None) or getattr(conn, "pageInfo", None)
        if page is None or not getattr(page, "has_next_page", getattr(page, "hasNextPage", False)):
            break
        cursor = getattr(page, "end_cursor", getattr(page, "endCursor", None))
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise provider_collection_incomplete("deezer")
        seen.add(cursor)
        after = cursor
    else:
        raise provider_collection_incomplete("deezer")
    _cache["playlists"] = (time.time(), out)
    return out


def _deezer_gql_client():
    from deezer_python_gql import DeezerGQLClient

    return DeezerGQLClient(arl=get_deezer_arl())


def fetch_tracks(playlist_id: str, *, force_refresh: bool = False):
    ts, data = _cache["tracks"].get(playlist_id, (0, None))
    if not force_refresh and data is not None and time.time() - ts < TRACKS_TTL:
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
    tracks = []
    advertised_total = None
    url = f"https://api.deezer.com/playlist/{playlist_id}/tracks"
    params = {"limit": 100, "index": 0}
    seen_urls = set()
    for _page_number in range(PROVIDER_PAGE_LIMIT):
        if not _trusted_provider_url(url, "api.deezer.com") or url in seen_urls:
            raise provider_pagination_invalid("deezer")
        seen_urls.add(url)
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
        except Exception as exc:
            raise provider_exception("deezer", exc) from None
        if not isinstance(d, dict) or not isinstance(d.get("data"), list):
            raise provider_collection_incomplete("deezer")
        if 'total' in d:
            if type(d['total']) is not int or d['total'] < 0 or (advertised_total is not None and advertised_total != d['total']):
                raise provider_collection_incomplete('deezer')
            advertised_total = d['total']
        for t in d["data"]:
            if not isinstance(t, dict) or "id" not in t:
                raise provider_collection_incomplete("deezer")
            tracks.append({
                "id": str(t["id"]), "title": t.get("title") or "?",
                "artist": (t.get("artist") or {}).get("name", ""),
                "album": (t.get("album") or {}).get("title", ""),
                "duration": t.get("duration") or 0,
            })
        next_url = d.get("next")
        if not next_url:
            break
        if not isinstance(next_url, str) or not _trusted_provider_url(next_url, "api.deezer.com"):
            raise provider_pagination_invalid("deezer")
        url, params = next_url, None
    else:
        raise provider_collection_incomplete("deezer")
    if advertised_total is not None and len(tracks) != advertised_total:
        raise provider_collection_incomplete('deezer')
    return tracks


async def _fetch_tracks_graphql(playlist_id: str) -> list:
    client = _deezer_gql_client()
    return await _fetch_tracks_graphql_with_client(client, playlist_id)


async def _fetch_tracks_graphql_with_client(client, playlist_id: str) -> list:
    tracks, after = [], None
    seen = set()
    for _page_number in range(PROVIDER_PAGE_LIMIT):
        try:
            pl = await client.get_playlist(playlist_id=playlist_id,
                                           tracks_first=100, tracks_after=after)
            conn = pl.tracks
            edges = conn.edges
        except Exception as exc:
            raise provider_exception("deezer", exc) from None
        if edges is None:
            raise provider_collection_incomplete("deezer")
        for e in edges:
            n = e.node
            if n is None or getattr(n, "id", None) is None:
                raise provider_collection_incomplete("deezer")
            artists = [c.node.name for c in n.contributors.edges
                       if c.node and "MAIN" in (c.roles or [])]
            tracks.append({
                "id": str(n.id), "title": n.title or "?",
                "artist": ", ".join(artists) or "",
                "album": (n.album.display_title if n.album else "") or "",
                "duration": n.duration or 0,
            })
        page = getattr(conn, "page_info", None) or getattr(conn, "pageInfo", None)
        if not page or not getattr(page, "has_next_page", getattr(page, "hasNextPage", False)):
            break
        cursor = getattr(page, "end_cursor", getattr(page, "endCursor", None))
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise provider_collection_incomplete("deezer")
        seen.add(cursor)
        after = cursor
    else:
        raise provider_collection_incomplete("deezer")
    return tracks


def _trusted_provider_url(url: str, hostname: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except Exception:
        return False


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
    # Configuration and first-run setup do not depend on provider availability.
    stored = bool(get_deezer_arl())
    user = (cfg.get('auth_accounts') or {}).get('deezer') if stored else None
    return {"music_root": cfg.get('music_root') or '',
            "music_root_configured": bool(cfg.get('music_root')) and Path(cfg['music_root']).is_absolute(),
            "arl_set": stored,
            "wav_mode": cfg.get("wav_mode", "source"),
            "numbering": cfg.get("numbering", True),
            "sc_user": cfg.get("sc_username") or None,
            "user": user}


@app.post("/api/config")
def api_set_config(c: ConfigIn):
    if c.music_root is not None:
        try:
            p = catalog_service.validate_root(c.music_root)
        except MusicRootRequired:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
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
    _require_music_root()
    try:
        pls = await fetch_playlists()
    except Exception as exc:
        raise provider_exception("deezer", exc) from None
    for p in pls:
        pl_dir = library.playlist_dir(p["id"], p["title"])
        sc = library.load_sidecar(pl_dir)
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif library.is_ready_entry(pl_dir, e):
                ok += 1
        p.update({"ok": ok, "errors": err, "path": str(pl_dir)})
    return pls


@app.get("/api/playlists/{playlist_id}/tracks")
def api_tracks(playlist_id: str, title: str = ""):
    _require_music_root()
    if playlist_id.startswith('local:'):
        from .rekordbox_service import source_tracks, SourceError
        try:
            pl_dir, tracks = source_tracks(playlist_id, title)
            return {'path': str(pl_dir), 'tracks': catalog_service.playlist_tracks(pl_dir, tracks)}
        except SourceError as exc:
            raise HTTPException(409, {'code': exc.code}) from None
    try:
        deezer_tracks = fetch_tracks(playlist_id)
    except Exception as exc:
        raise provider_exception("deezer", exc) from None
    pl_dir = library.playlist_dir(playlist_id, title)
    return {"path": str(pl_dir), "tracks": catalog_service.playlist_tracks(pl_dir, deezer_tracks)}


@app.post("/api/playlists/{playlist_id}/download")
def api_download(playlist_id: str, body: DownloadIn, title: str = "", mode: str = "append"):
    _require_music_root()
    if not body.tracks:
        raise HTTPException(400, "пустой список треков")
    return _enqueue_missing(playlist_id, title or playlist_id, body.tracks, mode)


class RenumberIn(BaseModel):
    order: list  # [track_id, ...] в нужном порядке
    total: int = 0


@app.post("/api/playlists/{key}/renumber")
def api_renumber(key: str, body: RenumberIn, title: str = ""):
    _require_music_root()
    pl_dir = library.playlist_dir(key, title or key)
    catalog_service.validate_destination(pl_dir, require_online=True)
    digits = library.digits_for(body.total or len(body.order))
    renamed = library.renumber_playlist(pl_dir, [str(i) for i in body.order], digits)
    return {"renamed": renamed}


@app.post("/api/playlists/{playlist_id}/bind")
def api_bind(playlist_id: str, body: BindIn):
    _require_music_root()
    try:
        p = catalog_service.validate_root(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
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


@app.post("/api/jobs/clear-completed")
def api_clear_completed_jobs():
    return jobs.clear_completed()


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    try:
        return jobs.cancel_job(job_id)
    except KeyError:
        raise HTTPException(404, "job не найден") from None
    except ValueError:
        raise HTTPException(409, {"code": "job_not_cancellable", "message": "Можно отменить только загрузку."}) from None


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, "job не найден")
    return j


# ---------- SoundCloud ----------
from . import soundcloud
from .deezer_client import save_config


def _all_sc_sources() -> list:
    return load_config().get("sc_sources", [])


def _sc_sources() -> list:
    cfg = load_config()
    account_id = str(cfg.get("sc_account_id") or "")
    return [
        source for source in cfg.get("sc_sources", [])
        if source.get("source") != "account"
        or (account_id and str(source.get("account_id") or "") == account_id)
    ]


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
    _require_music_root()
    out = []
    for s in _sc_sources():
        pl_dir = library.playlist_dir(_sc_key(s["id"]), s["title"])
        sc = library.load_sidecar(pl_dir)
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif library.is_ready_entry(pl_dir, e):
                ok += 1
        out.append({**s, "ok": ok, "errors": err, "path": str(pl_dir)})
    return out


@app.post("/api/sc/sources")
def api_sc_add(body: ScSourceIn):
    try:
        data = soundcloud.resolve(body.url)
    except Exception as exc:
        raise provider_exception("soundcloud", exc) from None
    sources = _all_sc_sources()
    if any(s["id"] == data["id"] for s in sources):
        raise HTTPException(409, "этот источник уже добавлен")
    src = {"id": data["id"], "url": body.url, "title": data["title"],
           "count": len(data["tracks"]), "source": "manual"}
    sources.append(src)
    _sc_save_sources(sources)
    return {**src, "errors": data.get("errors", [])}


@app.delete("/api/sc/sources/{source_id}")
def api_sc_delete(source_id: str):
    sources = _all_sc_sources()
    _sc_save_sources([s for s in sources if s["id"] != source_id])
    return {"ok": True}


@app.get("/api/sc/sources/{source_id}/tracks")
def api_sc_tracks(source_id: str):
    _require_music_root()
    src = next((s for s in _sc_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(404, "источник не найден")
    try:
        data = soundcloud.resolve(src["url"], use_cache=False)
    except Exception as exc:
        raise provider_exception("soundcloud", exc) from None
    pl_dir = library.playlist_dir(_sc_key(source_id), src["title"])
    tracks = [{**t, 'provider': t.get('provider') or 'sc'} for t in data['tracks']]
    return {"path": str(pl_dir), "tracks": catalog_service.playlist_tracks(pl_dir, tracks),
            "errors": data.get("errors", [])}


@app.post("/api/sc/sources/{source_id}/download")
def api_sc_download(source_id: str, body: DownloadIn, mode: str = "append"):
    _require_music_root()
    src = next((s for s in _sc_sources() if s["id"] == source_id), None)
    if not src:
        raise HTTPException(404, "источник не найден")
    tracks = [{**t, "provider": t.get("provider") or "sc"} for t in body.tracks]
    return _enqueue_missing(_sc_key(source_id), src["title"], tracks, mode)


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
    from .rekordbox_service import local_tracks, _local_sidecar, SourceError
    _require_music_root()
    out = []
    for s in _local_sources():
        key = f"local:{s['id']}"
        pl_dir = library.playlist_dir(key, s["title"])
        try:
            sc = _local_sidecar(pl_dir)
            count, membership_error = len(local_tracks(pl_dir)), None
        except SourceError as exc:
            sc, count, membership_error = {'tracks': {}}, None, {'code': exc.code}
        ok = err = 0
        for e in sc.get("tracks", {}).values():
            if e.get("status", "").startswith("verify_failed"):
                err += 1
            elif library.is_ready_entry(pl_dir, e):
                ok += 1
        out.append({**s, 'count': count, 'membership_error': membership_error,
                    "key": key, "ok": ok, "errors": err, "path": str(pl_dir)})
    return out


@app.post("/api/local/playlists")
def api_local_create(body: LocalPlaylistIn):
    _require_music_root()
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "пустое название")
    src = {"id": uuid.uuid4().hex[:8], "title": title,
           "url": (body.url or "").strip(), "count": 0, "local": True}
    sources = _local_sources()
    sources.append(src)
    _local_save(sources)
    from .rekordbox_service import persist_local_membership
    persist_local_membership(library.playlist_dir(f"local:{src['id']}", title), [])
    return {"ok": True, "key": f"local:{src['id']}", **src}


# ---------- Логин ----------
class LoginDeezerIn(BaseModel):
    arl: str


class LoginScIn(BaseModel):
    oauth_token: str


@app.post("/api/login/deezer")
def api_login_deezer(body: LoginDeezerIn):
    _require_legacy_login_mode()
    from .deezer_client import DeezerSession, _session_cache
    try:
        ds = DeezerSession(body.arl.strip())
    except Exception:
        raise HTTPException(401, "Deezer ARL was not accepted")
    set_deezer_arl(body.arl.strip())
    _session_cache["session"] = None  # сброс кеша сессии
    return {"id": ds.user["USER_ID"], "email": ds.user.get("EMAIL")}


@app.post("/api/login/soundcloud")
def api_login_sc(body: LoginScIn):
    _require_legacy_login_mode()
    token = body.oauth_token.strip()
    try:
        user = soundcloud.sc_validate(token)
    except Exception:
        raise HTTPException(401, "SoundCloud OAuth token was not accepted")
    set_soundcloud_oauth(token)
    cfg = load_config()
    cfg["sc_username"] = user.get("username", "")
    cfg["sc_account_id"] = str(user.get("id") or "")
    save_config(cfg)
    return {"id": user.get("id"), "username": user.get("username")}


@app.get("/api/sc/account")
def api_sc_account():
    token = soundcloud.sc_oauth_token()
    if not token:
        raise provider_error(
            "soundcloud", "provider_auth_required", "SoundCloud login is required",
            retryable=False, status_code=401,
        )
    playlists = soundcloud.sc_account_playlists(token)
    return {"playlists": playlists,
            "likes": {"id": "likes", "title": "❤ Лайки", "url": "https://soundcloud.com/you/likes"},
            "errors": playlists.errors}


class ScImportIn(BaseModel):
    items: list  # [{id,title,url}]


@app.post("/api/sc/account/import")
def api_sc_import(body: ScImportIn):
    cfg = load_config()
    account_id = str(cfg.get("sc_account_id") or "")
    sources = list(cfg.get("sc_sources", []))
    added = 0
    for it in body.items:
        if not it.get("url") or any(s["url"] == it["url"] and (
                s.get('source') != 'account' or str(s.get('account_id') or '') == account_id) for s in sources):
            continue
        sources.append({"id": str(it["id"]), "url": it["url"],
                        "title": it.get("title", "?"), "count": it.get("count", 0),
                        "source": "account", "account_id": account_id})
        added += 1
    _sc_save_sources(sources)
    return {"added": added}


@app.post("/api/sc/sync-account")
def api_sc_sync_account(force: bool = False):
    """Автосинк: подтягивает свои + лайкнутые плейлисты и лайки в источники.
    Не чаще раза в 5 минут (рейт-лимит SC), force — принудительно."""
    token = soundcloud.sc_oauth_token()
    if not token:
        raise provider_error(
            "soundcloud", "provider_auth_required", "SoundCloud login is required",
            retryable=False, status_code=401,
        )
    cfg = load_config()
    current_account = str(cfg.get("sc_account_id") or "")
    if (not force and cfg.get("sc_last_sync_account_id") == current_account
            and time.time() - cfg.get("sc_last_sync", 0) < 300):
        return {"added": 0, "total": len(_sc_sources()), "skipped": True}
    try:
        me = soundcloud.sc_validate(token)
        account = soundcloud.sc_account_playlists(token)
    except Exception as exc:
        raise provider_exception("soundcloud", exc) from None
    likes_url = f"https://soundcloud.com/{me.get('permalink', 'you')}/likes"
    items = [{"id": "likes", "title": "❤ Лайки", "url": likes_url, "count": me.get("likes_count", 0)}]
    items += account
    account_id = str(me.get("id") or account.account_id or "")
    cfg["sc_account_id"] = account_id
    sources = list(cfg.get("sc_sources", []))
    # вычищаем устаревший источник лайков (you/likes)
    sources = [s for s in sources if not (
        s["url"].endswith("/you/likes")
        and (s.get("source") != "account" or str(s.get("account_id") or "") == account_id)
    )]
    added = 0
    by_url = {
        s["url"]: s for s in sources
        if s.get("source") != "account" or str(s.get("account_id") or "") == account_id
    }
    for it in items:
        if not it.get("url"):
            continue
        existing = by_url.get(it["url"])
        if existing:  # обновляем название/счётчик (чинит старые битые данные)
            existing["title"] = it.get("title", existing["title"])
            existing["count"] = it.get("count", existing.get("count", 0))
            continue
        sources.append({"id": str(it["id"]), "url": it["url"],
                        "title": it.get("title", "?"), "count": it.get("count", 0),
                        "source": "account", "account_id": account_id})
        added += 1
    cfg["sc_sources"] = sources
    if not account.errors:
        cfg["sc_last_sync"] = time.time()
        cfg["sc_last_sync_account_id"] = account_id
    save_config(cfg)  # одна запись: и sources, и last_sync
    visible_total = sum(
        1 for source in sources
        if source.get("source") != "account" or str(source.get("account_id") or "") == account_id
    )
    return {"added": added, "total": visible_total, "errors": account.errors}


# ---------- Поиск (Deezer + SoundCloud) ----------
def _search_deezer(q: str) -> dict:
    out = {"tracks": [], "albums": [], "artists": [], "errors": []}
    try:
        d = _deezer_search_page("https://api.deezer.com/search", q, 10)
        out["tracks"] = [{"id": str(t["id"]), "title": t.get("title") or "?",
                          "artist": (t.get("artist") or {}).get("name", ""),
                          "album": (t.get("album") or {}).get("title", ""),
                          "duration": t.get("duration") or 0,
                          "url": t.get("link") or "", "provider": "deezer"}
                         for t in d.get("data", [])]
    except Exception as exc:
        out["errors"].append(detail_from_exception("deezer", exc))
    try:
        d = _deezer_search_page("https://api.deezer.com/search/album", q, 5)
        out["albums"] = [{"id": str(a["id"]), "title": a.get("title") or "?",
                          "artist": (a.get("artist") or {}).get("name", ""),
                          "url": a.get("link") or ""} for a in d.get("data", [])]
    except Exception as exc:
        out["errors"].append(detail_from_exception("deezer", exc))
    try:
        d = _deezer_search_page("https://api.deezer.com/search/artist", q, 5)
        out["artists"] = [{"id": str(a["id"]), "name": a.get("name") or "?",
                           "url": a.get("link") or ""} for a in d.get("data", [])]
    except Exception as exc:
        out["errors"].append(detail_from_exception("deezer", exc))
    return out


def _deezer_search_page(url: str, query: str, limit: int) -> dict:
    try:
        response = requests.get(url, params={"q": query, "limit": limit}, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise provider_exception("deezer", exc) from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise provider_collection_incomplete("deezer")
    return data


@app.get("/api/search")
def api_search(q: str, service: str = "both"):
    out = {}
    if service in ("both", "deezer"):
        out["deezer"] = _search_deezer(q)
    if service in ("both", "sc"):
        try:
            sc_res = soundcloud.search(q)
        except Exception as exc:
            sc_res = {"tracks": [], "albums": [], "artists": [], "errors": [detail_from_exception("soundcloud", exc)]}
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


async def _add_deezer_tracks(client, playlist_id: str, track_ids: list[str]) -> list[str]:
    requested = list(dict.fromkeys(track_ids))
    result = await client.add_tracks_to_playlist(playlist_id=playlist_id, track_ids=requested)
    if getattr(result, 'typename__', '') == 'PlaylistAddTracksError' or getattr(result, 'is_not_allowed', False) is True:
        raise provider_error('deezer', 'provider_access_denied', 'Deezer did not allow this operation', retryable=False, status_code=403)
    added = getattr(result, 'added_track_ids', None)
    if not isinstance(added, list) or {str(tid) for tid in added} != set(requested):
        # An uncertain/partial mutation is retried through membership reconciliation.
        raise provider_collection_incomplete('deezer')
    return list(dict.fromkeys(str(tid) for tid in added))


@app.post("/api/search/download")
async def api_search_download(body: SearchDownloadIn):
    """Скачать найденные треки в выбранный плейлист/папку.
    Если цель — плейлист Deezer, треки также добавляются в него на сервисе."""
    _require_music_root()
    if not body.tracks:
        raise HTTPException(400, "пустой список треков")
    target_key, target_title = body.target_key, body.target_title
    if body.target_dir:
        # произвольная папка: разовый биндинг dir:<hash> -> путь, sidecar живёт в ней
        try:
            p = catalog_service.validate_root(body.target_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        target_key = "dir:" + uuid.uuid4().hex[:8]
        library.bind_playlist(target_key, str(p))
        target_title = target_title or p.name
    local_result = _enqueue_missing(target_key, target_title, body.tracks, "append")
    added_remote = 0
    remote_action = None
    if not body.target_dir and _is_deezer_playlist_key(body.target_key):
        dz_ids = list(dict.fromkeys(
            str(t["id"]) for t in body.tracks if t.get("provider", "deezer") == "deezer"
        ))
        if dz_ids:
            remote_action = _remote_action_store.create_deezer_add(body.target_key, dz_ids)
            try:
                client = _deezer_gql_client()
                added_ids = await _add_deezer_tracks(client, body.target_key, dz_ids)
                added_remote = len(added_ids)
                remote_action = _remote_action_store.mark_succeeded(
                    remote_action["id"], added_track_ids=added_ids,
                )
                _cache["tracks"].pop(body.target_key, None)
            except Exception as exc:
                remote_action = _remote_action_store.mark_failed(
                    remote_action["id"], detail_from_exception("deezer", exc),
                )
    return {**local_result, "added_to_deezer": added_remote, "remote_action": remote_action}


@app.get("/api/remote-actions")
def api_remote_actions():
    return _remote_action_store.list()


@app.post("/api/remote-actions/{action_id}/retry")
async def api_remote_action_retry(action_id: str):
    action = _remote_action_store.get(action_id)
    if action is None:
        raise HTTPException(404, "remote action not found")
    if action.get("service") != "deezer" or action.get("operation") != "add_tracks_to_playlist":
        raise HTTPException(409, "remote action cannot be retried")
    action = _remote_action_store.mark_retrying(action_id)
    try:
        client = _deezer_gql_client()
        remote_tracks = await _fetch_tracks_graphql_with_client(client, action["target_id"])
        present = {str(track["id"]) for track in remote_tracks}
        already_present = [track_id for track_id in action["track_ids"] if track_id in present]
        missing = [track_id for track_id in action["track_ids"] if track_id not in present]
        if missing:
            await _add_deezer_tracks(client, action["target_id"], missing)
        _cache["tracks"].pop(action["target_id"], None)
        return _remote_action_store.mark_succeeded(
            action_id,
            added_track_ids=missing,
            already_present_track_ids=already_present,
        )
    except Exception as exc:
        error = detail_from_exception("deezer", exc)
        _remote_action_store.mark_failed(action_id, error)
        raise provider_exception("deezer", exc) from None


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
        client = _deezer_gql_client()
        added = await _add_deezer_tracks(client, body.playlist_id, [str(i) for i in body.track_ids])
        _cache["playlists"] = (0, None)
        _cache["tracks"].pop(body.playlist_id, None)
        return {"ok": True, "added": len(added)}
    except Exception as exc:
        raise provider_exception("deezer", exc) from None


@app.post("/api/deezer/playlist/create")
async def api_dz_create(body: DzCreateIn):
    """Создать плейлист в Deezer (+ опционально сразу добавить треки)."""
    try:
        client = _deezer_gql_client()
        pl = await client.create_playlist(title=body.title, is_private=False, is_collaborative=False)
        inner = getattr(pl, "playlist", pl)
        pid = str(getattr(inner, "id"))
        if body.track_ids:
            await _add_deezer_tracks(client, pid, [str(i) for i in body.track_ids])
        _cache["playlists"] = (0, None)
        return {"ok": True, "id": pid, "title": body.title}
    except Exception as exc:
        raise provider_exception("deezer", exc) from None


@app.get("/api/deezer/album/{album_id}")
def api_dz_album(album_id: str):
    """Треки альбома Deezer для скачивания из поиска."""
    url, params = f"https://api.deezer.com/album/{album_id}", None
    seen, tracks, album_title = set(), [], ""
    for page_number in range(PROVIDER_PAGE_LIMIT):
        if not _trusted_provider_url(url, "api.deezer.com") or url in seen:
            raise provider_pagination_invalid("deezer")
        seen.add(url)
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise provider_exception("deezer", exc) from None
        if not isinstance(data, dict):
            raise provider_collection_incomplete("deezer")
        if page_number == 0:
            album_title = data.get("title") or ""
            connection = data.get("tracks")
        else:
            connection = data
        if not isinstance(connection, dict) or not isinstance(connection.get("data"), list):
            raise provider_collection_incomplete("deezer")
        for track in connection["data"]:
            if not isinstance(track, dict) or track.get("id") is None:
                raise provider_collection_incomplete("deezer")
            tracks.append({
                "id": str(track["id"]), "title": track.get("title") or "?",
                "artist": (track.get("artist") or {}).get("name", ""),
                "album": album_title,
                "duration": track.get("duration") or 0, "provider": "deezer",
            })
        next_url = connection.get("next")
        if not next_url:
            return tracks
        if not isinstance(next_url, str) or not _trusted_provider_url(next_url, "api.deezer.com"):
            raise provider_pagination_invalid("deezer")
        url = next_url
    raise provider_collection_incomplete("deezer")


@app.get("/api/sc/resolve-tracks")
def api_sc_resolve_tracks(url: str):
    """Треки сета/страницы SC для скачивания из поиска."""
    try:
        data = soundcloud.resolve(url)
    except Exception as exc:
        raise provider_exception("soundcloud", exc) from None
    return [{**t, "provider": "sc"} for t in data["tracks"]]


# ---------- Rekordbox ----------
from . import rekordbox as rb


@app.get("/api/rb/status")
def api_rb_status():
    out = {"db_exists": rb.db_exists(), "running": None, "playlists": [],
           "recovery": rb.get_recovery_status()}
    try:
        out['running'] = rb.rb_running()
    except rb.AdapterError as exc:
        out['error'] = {'code': str(exc)}
        return out
    if out["db_exists"] and not out["running"]:
        try:
            out["playlists"] = rb.get_rb_playlists()
        except Exception:
            out["error"] = "Rekordbox status unavailable"
    return out


class RbRecoveryIn(BaseModel):
    expected_plan_hash: str | None = None


@app.get('/api/rb/recovery')
def api_rb_recovery_preview():
    return rb.recover_operation()


@app.post('/api/rb/recovery')
def api_rb_recovery_restore(body: RbRecoveryIn, confirmation_token: str | None = None):
    return rb.recover_operation(dry_run=False, expected_plan_hash=body.expected_plan_hash,
                                confirmation_token=confirmation_token)


class RbSyncIn(BaseModel):
    playlist_key: str
    playlist_title: str
    expected_plan_hash: str | None = None
    playlist_id: str | None = None


def _desired_tracks_for_rekordbox(playlist_key: str, playlist_title: str) -> list[dict]:
    from .rekordbox_service import resolve
    return resolve(playlist_key, playlist_title)


@app.post("/api/rb/sync")
def api_rb_sync(body: RbSyncIn, dry_run: bool = True, confirmation_token: str | None = None):
    from .rekordbox_service import sync
    return sync(body.playlist_key, body.playlist_title, dry_run=dry_run,
                confirmation_token=confirmation_token, expected_plan_hash=body.expected_plan_hash,
                playlist_id=body.playlist_id)


class FlipIn(RbSyncIn):
    to_wav: bool
    workers: int = 0


@app.post("/api/flip")
def api_flip(body: FlipIn, dry_run: bool = True, confirmation_token: str | None = None):
    from .rekordbox_service import sync
    return sync(body.playlist_key, body.playlist_title, dry_run=dry_run,
                confirmation_token=confirmation_token, expected_plan_hash=body.expected_plan_hash,
                playlist_id=body.playlist_id, to_wav=body.to_wav)


class PrepareWavIn(BaseModel):
    playlist_key: str
    playlist_title: str
    bit_depth: int = 16


@app.post('/api/rb/prepare-wav')
def api_rb_prepare_wav(body: PrepareWavIn):
    from .rekordbox_service import prepare_wav
    return prepare_wav(body.playlist_key, body.playlist_title, body.bit_depth)


@app.get('/api/rb/media-state')
def api_rb_media_state(playlist_key: str, playlist_title: str, playlist_id: str | None = None):
    from .rekordbox_service import inspect_state
    return inspect_state(playlist_key, playlist_title, playlist_id)


@app.get("/api/errors")
async def api_errors(include_status: bool = False):
    """Все треки со статусом verify_failed_* по всем плейлистам и источникам."""
    out = []
    warnings = []
    try:
        playlists = await fetch_playlists()
    except Exception as exc:
        warnings.append(detail_from_exception('deezer', exc))
        playlists = []
    entries = [(p["id"], p["title"], "deezer") for p in playlists]
    entries += [(_sc_key(s["id"]), s["title"], "sc") for s in _sc_sources()]
    entries += [(f"local:{s['id']}", s["title"], "local") for s in _local_sources()]

    for key, title, provider in entries:
        pl_dir = library.playlist_dir(key, title)
        sc = library.load_sidecar(pl_dir)
        for tid, e in sc.get("tracks", {}).items():
            if str(e.get("status", "")).startswith("verify_failed"):
                track_provider = str(e.get("provider") or provider)
                raw_id = str(tid)
                prefix = f"{track_provider}:"
                if raw_id.startswith(prefix):
                    raw_id = raw_id[len(prefix):]
                retry_stage = str(e.get("status", "")).removeprefix("verify_failed_")
                out.append({
                    "playlist_key": key, "playlist_title": title, "provider": track_provider,
                    "error": e.get("error", ""), "file": e.get("file", ""),
                    "status": e["status"],
                    "track": {"id": raw_id, "title": e.get("title", "?"),
                              "provider": track_provider, "retry_stage": retry_stage,
                              "artist": e.get("artist", ""),
                              "duration": e.get("duration_expected", 0),
                              "url": e.get("url", ""),
                              "position": e.get("position")},
                })
    if include_status:
        return {'items': out, 'errors': warnings}
    if warnings:
        raise provider_error('deezer', warnings[0]['code'], warnings[0]['message'], retryable=warnings[0]['retryable'], status_code=503)
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
    _require_music_root()
    catalog_service.validate_destination(library.playlist_dir(body.playlist_key, body.playlist_title), require_online=True)
    provider = str(body.track.get("provider") or (
        "sc" if body.playlist_key.startswith("sc:") else "deezer"
    ))
    raw_id = str(body.track.get("id", ""))
    prefix = f"{provider}:"
    if raw_id.startswith(prefix):
        raw_id = raw_id[len(prefix):]
    track = {**body.track, "id": raw_id, "provider": provider}
    # если у трека была позиция — восстанавливаем её, а не кидаем вниз
    mode = "playlist_order" if track.get("position") else "append"
    job_id = jobs.enqueue(body.playlist_key, body.playlist_title, [track], mode=mode)
    return {"job_id": job_id}


# ---------- статика ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
