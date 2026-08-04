# -*- coding: utf-8 -*-
"""Запись аудио-тегов и обложек после скачивания (FLAC/MP3/M4A)."""
import logging
from pathlib import Path

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, TSRC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0"}


def deezer_cover_url(picture_id: str, size: int = 500) -> str:
    return f"https://e-cdns-images.dzcdn.net/images/cover/{picture_id}/{size}x{size}-000000-80-0-0.jpg"


def _fetch_cover(url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.ok and len(r.content) > 1000:
            return r.content
    except Exception as e:
        log.warning("cover fetch failed: %s", e)
    return None


def _meta_from_deezer(infos: dict) -> dict:
    return {
        "title": infos.get("SNG_TITLE", ""),
        "artist": infos.get("ART_NAME", ""),
        "album": infos.get("ALB_TITLE", ""),
        "album_artist": infos.get("ART_NAME", ""),
        "isrc": infos.get("ISRC", ""),
        "date": (infos.get("DIGITAL_RELEASE_DATE") or "")[:4],
        "track_number": infos.get("TRACK_NUMBER", ""),
        "cover": None,  # обложки не встраиваем: в WAV не переносятся, раздувают файл
    }


def _meta_from_sc(info: dict, cover_bytes: bytes | None) -> dict:
    return {
        "title": info.get("title", ""),
        "artist": info.get("uploader") or info.get("artist") or "",
        "album": "",
        "album_artist": "",
        "isrc": "",
        "date": (info.get("upload_date") or "")[:4],
        "track_number": "",
        "cover": None,
    }


def write_tags(fpath: Path, meta: dict):
    """Пишет теги + обложку. meta — словарь из _meta_from_*."""
    ext = fpath.suffix.lower()
    try:
        if ext == ".flac":
            _tag_flac(fpath, meta)
        elif ext == ".mp3":
            _tag_mp3(fpath, meta)
        elif ext in (".m4a", ".mp4", ".aac"):
            _tag_mp4(fpath, meta)
        # wav: теги не пишем — Rekordbox берёт из своей базы
    except Exception as e:
        log.warning("tag write failed for %s: %s", fpath, e)


def _tag_flac(fpath: Path, m: dict):
    audio = FLAC(str(fpath))
    audio["TITLE"] = m["title"]
    audio["ARTIST"] = m["artist"]
    if m["album"]:
        audio["ALBUM"] = m["album"]
    if m["album_artist"]:
        audio["ALBUMARTIST"] = m["album_artist"]
    if m["isrc"]:
        audio["ISRC"] = m["isrc"]
    if m["date"]:
        audio["DATE"] = m["date"]
    if m["track_number"]:
        audio["TRACKNUMBER"] = str(m["track_number"])
    if m["cover"]:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = m["cover"]
        audio.add_picture(pic)
    audio.save()


def _tag_mp3(fpath: Path, m: dict):
    try:
        audio = MP3(str(fpath))
    except Exception:
        from mutagen.id3 import ID3
        audio = MP3(str(fpath))
        audio.add_tags()
    audio["TIT2"] = TIT2(encoding=3, text=m["title"])
    audio["TPE1"] = TPE1(encoding=3, text=m["artist"])
    if m["album"]:
        audio["TALB"] = TALB(encoding=3, text=m["album"])
    if m["album_artist"]:
        audio["TPE2"] = TPE2(encoding=3, text=m["album_artist"])
    if m["isrc"]:
        audio["TSRC"] = TSRC(encoding=3, text=m["isrc"])
    if m["date"]:
        audio["TDRC"] = TDRC(encoding=3, text=m["date"])
    if m["track_number"]:
        audio["TRCK"] = TRCK(encoding=3, text=str(m["track_number"]))
    if m["cover"]:
        audio["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3,
                             desc="Cover", data=m["cover"])
    audio.save()


def _tag_mp4(fpath: Path, m: dict):
    audio = MP4(str(fpath))
    audio["\xa9nam"] = m["title"]
    audio["\xa9ART"] = m["artist"]
    if m["album"]:
        audio["\xa9alb"] = m["album"]
    if m["album_artist"]:
        audio["aART"] = m["album_artist"]
    if m["date"]:
        audio["\xa9day"] = m["date"]
    if m["track_number"]:
        audio["trkn"] = [(int(m["track_number"]), 0)] if str(m["track_number"]).isdigit() else []
    if m["cover"]:
        audio["covr"] = [MP4Cover(m["cover"], imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
