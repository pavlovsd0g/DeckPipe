# -*- coding: utf-8 -*-
"""Deezer-клиент: ARL -> GW (метаданные) + media API (скачивание) + верификация."""
import functools
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import imageio_ffmpeg
from Crypto.Cipher import Blowfish
from mutagen.flac import FLAC as MutagenFLAC
from mutagen.mp3 import MP3 as MutagenMP3
from mutagen.mp4 import MP4 as MutagenMP4
from mutagen.wave import WAVE as MutagenWAVE
from mutagen.oggopus import OggOpus as MutagenOpus

from .atomic_io import atomic_write_json, make_staged_path
from .download_control import DownloadCancelled, DownloadCleanupFailed, check_cancelled, cleanup_download_stages as cleanup_owned_stages, register_stage, run_media_process

_MUTAGEN_BY_EXT = {".flac": MutagenFLAC, ".mp3": MutagenMP3, ".m4a": MutagenMP4,
                   ".aac": MutagenMP4, ".mp4": MutagenMP4, ".wav": MutagenWAVE,
                   ".opus": MutagenOpus, ".ogg": MutagenOpus}

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BLOWFISH_SECRET = "g4el58wc0zvf9na1"
QUALITIES = ["FLAC", "MP3_320", "MP3_128"]
FILESIZE_KEY = {"FLAC": "FILESIZE_FLAC", "MP3_320": "FILESIZE_MP3_320", "MP3_128": "FILESIZE_MP3_128"}


def _data_dir() -> Path:
    """Per-user app data; tests may inject DECKPIPE_DATA_DIR."""
    override = os.environ.get("DECKPIPE_DATA_DIR") if not getattr(sys, "frozen", False) else None
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            d = Path(base) / "DeckPipe"
        else:
            raise RuntimeError("DeckPipe data directory is unavailable")
    return d


ROOT = _data_dir()
CONFIG_PATH = ROOT / "config.local.json"
SECRET_STORE_PATH = ROOT / "secrets.dpapi"
SECRET_CONFIG_FIELDS = ("arl", "sc_oauth")
TELEGRAM_SECRET_CONFIG_FIELDS = ("bot_token",)


def _sanitize_config(cfg: dict) -> dict:
    sanitized = dict(cfg)
    for field in SECRET_CONFIG_FIELDS:
        sanitized.pop(field, None)
    telegram = sanitized.get("telegram")
    if isinstance(telegram, dict):
        telegram = dict(telegram)
        for field in TELEGRAM_SECRET_CONFIG_FIELDS:
            telegram.pop(field, None)
        if telegram:
            sanitized["telegram"] = telegram
        else:
            sanitized.pop("telegram", None)
    return sanitized


def _plaintext_secret_fields(cfg: dict) -> list[str]:
    fields = [field for field in SECRET_CONFIG_FIELDS if field in cfg]
    telegram = cfg.get("telegram")
    if isinstance(telegram, dict):
        fields.extend(f"telegram.{field}" for field in TELEGRAM_SECRET_CONFIG_FIELDS if field in telegram)
    return sorted(fields)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            # старый конфиг мог быть прочитан/переписан в cp1251 — чиним
            cfg = json.loads(CONFIG_PATH.read_text(encoding="cp1251"))
    else:
        cfg = {}
    return _sanitize_config(cfg)


def save_config(cfg: dict):
    cfg = dict(cfg)
    reserved = _plaintext_secret_fields(cfg)
    if reserved:
        raise ValueError("config.local.json cannot store credential fields")
    atomic_write_json(CONFIG_PATH, cfg, backup=False)


def _secure_store():
    from .secure_store import SecureCredentialStore

    return SecureCredentialStore(SECRET_STORE_PATH)


def get_deezer_arl() -> str | None:
    return _secure_store().get_deezer_arl()


def set_deezer_arl(arl: str) -> None:
    _secure_store().set_deezer_arl(arl)


def get_soundcloud_oauth() -> str | None:
    return _secure_store().get_soundcloud_oauth()


def set_soundcloud_oauth(token: str) -> None:
    _secure_store().set_soundcloud_oauth(token)


def get_telegram_bot_token() -> str | None:
    return _secure_store().get_telegram_bot_token()


def set_telegram_bot_token(token: str) -> None:
    _secure_store().set_telegram_bot_token(token)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\.deckpipe-stage-", "_", name, flags=re.IGNORECASE)
    name = re.sub(r"\.part(?=\.|$)", "_", name, flags=re.IGNORECASE)
    return name.strip().strip(".")[:180] or "track"


class DeezerSession:
    def __init__(self, arl: str):
        self.s = requests.Session()
        self.s.cookies.set("arl", arl, domain=".deezer.com")
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        ud = self.gw("deezer.getUserData", token="null")
        self.token = ud["checkForm"]
        self.user = ud["USER"]
        if not int(self.user.get("USER_ID", 0)):
            raise RuntimeError("ARL недействителен (USER_ID=0)")
        self.license_token = self.user["OPTIONS"]["license_token"]

    def gw(self, method: str, token: str = None, **params):
        r = self.s.post(
            "https://www.deezer.com/ajax/gw-light.php",
            params={"api_version": "1.0", "api_token": token or self.token,
                    "method": method, "input": "3"},
            json=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        err = data.get("error")
        if err and (err if isinstance(err, str) else any(err.values())):
            raise RuntimeError(f"GW {method}: {err}")
        return data.get("results")

    def media_url(self, track_token: str, quality: str) -> str:
        order = [quality] + [q for q in QUALITIES if q != quality]
        payload = {"license_token": self.license_token,
                   "media": [{"type": "FULL",
                              "formats": [{"cipher": "BF_CBC_STRIPE", "format": q}
                                          for q in order]}],
                   "track_tokens": [track_token]}
        r = self.s.post("https://media.deezer.com/v1/get_url", json=payload, timeout=30)
        r.raise_for_status()
        for m in r.json()["data"][0]["media"]:
            if m["format"] == quality:
                return m["sources"][0]["url"]
        raise RuntimeError(f"media API не выдал {quality}")

    def download_track(self, track_id: str, out_dir: Path, prefer: str = "FLAC"):
        """Скачивает трек с fallback по качеству. Возвращает (path, quality, infos)."""
        check_cancelled()
        infos = self.gw("song.getData", SNG_ID=track_id)
        check_cancelled()
        title, artist = infos["SNG_TITLE"], infos["ART_NAME"]
        order = [prefer] + [q for q in QUALITIES if q != prefer]
        for q in order:
            check_cancelled()
            if int(infos.get(FILESIZE_KEY[q], "0") or 0) == 0:
                continue
            try:
                url = self.media_url(infos["TRACK_TOKEN"], q)
                check_cancelled()
                ext = "flac" if q == "FLAC" else "mp3"
                final_path = out_dir / f"{sanitize_filename(artist + ' - ' + title)}.{ext}"
                fpath = make_staged_path(final_path)
                register_stage(fpath)
                key = _blowfish_key(track_id)
                try:
                    with self.s.get(url, stream=True, timeout=(15, 5)) as r:
                        r.raise_for_status()
                        check_cancelled()
                        i = 0
                        with open(fpath, "wb") as f:
                            for chunk in r.iter_content(chunk_size=2048):
                                check_cancelled()
                                if not chunk:
                                    continue
                                if i % 3 == 0 and len(chunk) == 2048:
                                    chunk = Blowfish.new(
                                        key, Blowfish.MODE_CBC,
                                        b"\x00\x01\x02\x03\x04\x05\x06\x07").decrypt(chunk)
                                f.write(chunk)
                                i += 1
                            f.flush()
                            os.fsync(f.fileno())
                    check_cancelled()
                except Exception:
                    cleanup_owned_stages(fpath)
                    raise
                return fpath, q, infos
            except (DownloadCancelled, DownloadCleanupFailed):
                raise
            except Exception:
                check_cancelled()
        raise RuntimeError("download failed")


def _blowfish_key(track_id: str) -> bytes:
    h = hashlib.md5(str(track_id).encode()).hexdigest()
    return "".join(
        chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
        for t in zip(h[:16], h[16:], BLOWFISH_SECRET)
    ).encode()


def verify_file(fpath: Path, expected_duration: int, tolerance: float = 2.0):
    """Проверка целостности: читаемость + полное декодирование + длительность.
    Возвращает (ok, error_message, actual_duration)."""
    check_cancelled()
    try:
        if not fpath.exists() or fpath.stat().st_size < 30_000:
            return False, "файл отсутствует или подозрительно мал", 0.0
        cls = _MUTAGEN_BY_EXT.get(fpath.suffix.lower())
        if cls is None:
            return False, f"неизвестный формат {fpath.suffix}", 0.0
        mf = cls(str(fpath))
        actual = float(mf.info.length)
    except Exception:
        return False, "media validation failed", 0.0
    r = run_media_process([FFMPEG, "-v", "error", "-i", str(fpath), "-f", "null", "-"])
    if r.returncode != 0 or r.stderr.strip():
        return False, "media validation failed", actual
    if abs(actual - expected_duration) > tolerance:
        return False, f"длительность {actual:.1f}c != ожидаемая ~{expected_duration}c (обрезан?)", actual
    return True, "", actual


_session_cache = {"session": None, "arl": None, "ts": 0}


def get_session() -> DeezerSession:
    """Кешированная сессия (пересоздаём, если ARL сменился или прошло >10 мин)."""
    arl = get_deezer_arl()
    if not arl:
        raise RuntimeError("Deezer login is not configured")
    now = time.time()
    if (_session_cache["session"] is None or _session_cache["arl"] != arl
            or now - _session_cache["ts"] > 600):
        _session_cache["session"] = DeezerSession(arl)
        _session_cache["arl"] = arl
    _session_cache["ts"] = now
    return _session_cache["session"]
