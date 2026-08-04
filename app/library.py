# -*- coding: utf-8 -*-
"""Локальная библиотека: пути, sidecar-файлы, статусы треков."""
import json
import re
import time
import unicodedata
from pathlib import Path

from .deezer_client import load_config, save_config, sanitize_filename

SIDECAR_NAME = ".deckpipe.json"
DEFAULT_ROOT = Path.home() / "Music" / "DeckPipe"
AUDIO_EXTS = (".flac", ".mp3", ".wav", ".aiff", ".m4a")


def music_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("music_root") or DEFAULT_ROOT)


def set_music_root(path: str):
    cfg = load_config()
    cfg["music_root"] = path
    save_config(cfg)


def playlist_dir(playlist_id: str, title: str = "") -> Path:
    """Путь папки плейлиста: кастомный (bindings) или MUSIC_ROOT/<title>."""
    cfg = load_config()
    bindings = cfg.get("bindings", {})
    if playlist_id in bindings:
        return Path(bindings[playlist_id])
    return music_root() / sanitize_filename(title or playlist_id)


def bind_playlist(playlist_id: str, path: str):
    cfg = load_config()
    cfg.setdefault("bindings", {})[playlist_id] = path
    save_config(cfg)


def sidecar_path(pl_dir: Path) -> Path:
    return pl_dir / SIDECAR_NAME


def load_sidecar(pl_dir: Path) -> dict:
    p = sidecar_path(pl_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tracks": {}}


def save_sidecar(pl_dir: Path, data: dict):
    pl_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path(pl_dir).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_track_status(pl_dir: Path, deezer_id: str, entry: dict):
    sc = load_sidecar(pl_dir)
    sc.setdefault("tracks", {})[str(deezer_id)] = entry
    save_sidecar(pl_dir, sc)


# ---------- нумерация треков ----------

def digits_for(total: int) -> int:
    return 3 if total >= 100 else 2


def strip_number_prefix(filename: str) -> str:
    """убрать 'NN - ' / 'NN. ' из начала имени файла."""
    stem, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
    stem = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", stem)
    return f"{stem}.{ext}" if ext else stem


def max_position(sidecar_tracks: dict) -> int:
    return max((int(e.get("position", 0) or 0) for e in sidecar_tracks.values()), default=0)


def numbered_name(num: int, digits: int, artist: str, title: str, ext: str) -> str:
    base = sanitize_filename(f"{artist} - {title}") if artist else sanitize_filename(title)
    return f"{num:0{digits}d} - {base}.{ext}"


def renumber_playlist(pl_dir: Path, ordered_ids: list, digits: int) -> int:
    """Перенумеровывает файлы по порядку ordered_ids. Возвращает число переименований."""
    sc = load_sidecar(pl_dir)
    tracks = sc.get("tracks", {})
    renamed = 0
    for i, tid in enumerate(ordered_ids, start=1):
        e = tracks.get(str(tid))
        if not e or not e.get("file"):
            continue
        old = pl_dir / e["file"]
        if not old.exists():
            continue
        new_name = f"{i:0{digits}d} - {strip_number_prefix(e['file'])}"
        if e["file"] == new_name and e.get("position") == i:
            continue
        new = pl_dir / new_name
        if new.exists() and new != old:
            continue  # не перетираем чужой файл
        old.rename(new)
        e["file"] = new_name
        e["position"] = i
        renamed += 1
    if renamed:
        save_sidecar(pl_dir, sc)
    return renamed


# ---------- нормализация и нечёткое сопоставление файлов ----------

def _normalize(s: str) -> str:
    """нижний регистр, без диакритики, пунктуация -> пробел, схлопнуть пробелы."""
    s = s.replace("'", "").replace("’", "").replace("‘", "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strip_track_number(stem: str) -> str:
    """убрать ведущий номер трека: '01 - X', '01. X', '1_X'."""
    return re.sub(r"^\s*\d{1,3}\s*[-._)]?\s*", "", stem)


def match_file(track_title: str, track_artist: str, files: list) -> Path | None:
    """Нечёткий подбор файла под трек. files: [Path]. Возвращает Path или None."""
    n_title = _normalize(track_title)
    # вариант названия без скобок: 'Song (Radio Edit)' -> 'song'
    n_title_short = _normalize(re.sub(r"[\(\[].*?[\)\]]", "", track_title))
    n_artist = _normalize(track_artist)
    first_artist = _normalize(re.split(r"[,;&]| feat\.? | ft\.? | vs\.? | x ", track_artist)[0])

    best, best_score = None, 0
    for f in files:
        stem = _normalize(_strip_track_number(f.stem))
        if not stem:
            continue
        score = 0
        # совпадение названия
        if n_title and stem == n_title:
            score += 5
        elif n_title and n_title in stem:
            score += 3
        elif n_title_short and len(n_title_short) >= 4 and n_title_short in stem:
            score += 2
        elif stem in n_title:  # файл назван короче
            score += 1
        if score == 0:
            continue
        # совпадение исполнителя — сильный буст и разрешитель неоднозначности
        if n_artist and n_artist in stem:
            score += 3
        elif first_artist and first_artist in stem:
            score += 2
        if score > best_score:
            best, best_score = f, score
    # минимальный порог: название (2-3) — обязательно; без артиста берём только если кандидат один
    if best and best_score >= 3:
        if best_score >= 5:
            return best
        # без совпадения артиста — только если файл один на всё название
        same = [f for f in files
                if (n_title and n_title in _normalize(_strip_track_number(f.stem)))]
        if len(same) == 1:
            return best
    return None


def scan_playlist(pl_dir: Path, deezer_tracks: list) -> list:
    """Статусы треков плейлиста: ok / error / missing (+ подхват файлов без sidecar)."""
    sc = load_sidecar(pl_dir)
    sidecar_tracks = sc.get("tracks", {})
    changed = False
    result = []
    disk_files = [f for f in pl_dir.glob("*")
                  if f.suffix.lower() in AUDIO_EXTS] if pl_dir.exists() else []
    used_files = set()

    for t in deezer_tracks:
        tid = str(t["id"])
        entry = sidecar_tracks.get(tid)
        if entry:
            f = pl_dir / entry["file"]
            if entry.get("status", "").startswith("verify_failed"):
                status, err = "error", entry.get("error", "")
            elif f.exists():
                status, err = "ok", ""
                used_files.add(f.resolve())
            else:
                status, err = "missing", ""
                entry["status"] = "missing"
                changed = True
            fmt = entry.get("format", "")
            fname = entry["file"]
        else:
            # подхват: ищем файл на диске нечётким матчингом
            found = match_file(t["title"], t["artist"],
                               [f for f in disk_files if f.resolve() not in used_files])
            if found:
                status, err = "ok", ""
                fmt = found.suffix.lstrip(".").lower()
                fname = found.name
                used_files.add(found.resolve())
                sidecar_tracks[tid] = {
                    "title": t["title"], "artist": t["artist"], "file": fname,
                    "format": fmt, "status": "ok", "downloaded_at": int(time.time()),
                    "adopted": True,
                }
                changed = True
            else:
                status, err, fmt, fname = "missing", "", "", ""
        result.append({**t, "status": status, "error": err, "format": fmt, "file": fname,
                       "flipped": bool(entry and entry.get("flipped_to") == "wav"),
                       "mp3_source": bool(entry and entry.get("mp3_source"))})

    if changed:
        save_sidecar(pl_dir, sc)
    return result
