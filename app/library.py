# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import time
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_load_json, atomic_write_json, file_lock, is_partial_path
from .deezer_client import load_config, save_config, sanitize_filename

SIDECAR_NAME = ".deckpipe.json"
DEFAULT_ROOT = Path.home() / "Music" / "DeckPipe"
AUDIO_EXTS = (".flac", ".mp3", ".wav", ".aiff", ".m4a", ".aac", ".opus", ".ogg")

_last_scan_counters = {"enumerations": 0, "normalized_stems": 0, "candidate_checks": 0}


def music_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("music_root") or DEFAULT_ROOT)


def set_music_root(path: str):
    cfg = load_config()
    cfg["music_root"] = path
    save_config(cfg)


def playlist_dir(playlist_id: str, title: str = "") -> Path:
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
    return Path(pl_dir) / SIDECAR_NAME


def track_key(track_id: object, provider: str | None = None) -> str:
    provider = provider or "deezer"
    raw = str(track_id)
    if provider == "deezer":
        return raw
    if raw.startswith(f"{provider}:"):
        return raw
    return f"{provider}:{raw}"


def _validate_sidecar(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid sidecar")
    tracks = payload.get("tracks", {})
    if not isinstance(tracks, dict):
        raise ValueError("invalid sidecar")
    normalized = dict(payload)
    normalized["tracks"] = {}
    for key, entry in tracks.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("invalid sidecar")
        normalized["tracks"][key] = dict(entry)
    return normalized


def load_sidecar(pl_dir: Path) -> dict:
    path = sidecar_path(pl_dir)
    if not path.exists() and not Path(f"{path}.bak").exists():
        return {"tracks": {}}
    return atomic_load_json(
        path,
        default={"tracks": {}},
        validator=_validate_sidecar,
        backup=True,
    )


def save_sidecar(pl_dir: Path, data: dict):
    Path(pl_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(sidecar_path(pl_dir), data, validator=_validate_sidecar, backup=True)


def update_track_status(pl_dir: Path, deezer_id: str, entry: dict):
    provider = entry.get("provider", "deezer") if isinstance(entry, dict) else "deezer"
    key = track_key(deezer_id, provider)
    with file_lock(sidecar_path(pl_dir)):
        sc = load_sidecar(pl_dir)
        current = dict(sc.setdefault("tracks", {}).get(key, {}))
        current.update(dict(entry))
        sc.setdefault("tracks", {})[key] = current
        save_sidecar(pl_dir, sc)


def is_ready_entry(pl_dir: Path, entry: dict) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return False
    name = entry.get("file", "")
    if not isinstance(name, str) or not name or is_partial_path(name):
        return False
    rel = Path(name)
    if rel.is_absolute() or rel.name != name:
        return False
    candidate = Path(pl_dir) / rel
    try:
        root = Path(pl_dir).resolve()
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            return False
    except Exception:
        return False
    return candidate.is_file()


def get_last_scan_counters() -> dict[str, int]:
    return dict(_last_scan_counters)


def digits_for(total: int) -> int:
    return 3 if total >= 100 else 2


def strip_number_prefix(filename: str) -> str:
    stem, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
    stem = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", stem)
    return f"{stem}.{ext}" if ext else stem


def max_position(sidecar_tracks: dict) -> int:
    return max((int(e.get("position", 0) or 0) for e in sidecar_tracks.values()), default=0)


def numbered_name(num: int, digits: int, artist: str, title: str, ext: str) -> str:
    base = sanitize_filename(f"{artist} - {title}") if artist else sanitize_filename(title)
    return f"{num:0{digits}d} - {base}.{ext}"


def renumber_playlist(pl_dir: Path, ordered_ids: list, digits: int) -> int:
    with file_lock(sidecar_path(pl_dir)):
        sc = load_sidecar(pl_dir)
        tracks = sc.get("tracks", {})
        renamed = 0
        for i, tid in enumerate(ordered_ids, start=1):
            e = tracks.get(track_key(tid)) or tracks.get(str(tid))
            if not e or not e.get("file") or is_partial_path(e["file"]):
                continue
            old = Path(pl_dir) / e["file"]
            if not old.exists():
                continue
            new_name = f"{i:0{digits}d} - {strip_number_prefix(e['file'])}"
            if e["file"] == new_name and e.get("position") == i:
                continue
            new = Path(pl_dir) / new_name
            if new.exists() and new != old:
                continue
            old.rename(new)
            e["file"] = new_name
            e["position"] = i
            renamed += 1
        if renamed:
            save_sidecar(pl_dir, sc)
        return renamed


def _normalize(s: str) -> str:
    s = s.replace("'", "").replace("’", "").replace("‘", "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strip_track_number(stem: str) -> str:
    return re.sub(r"^\s*\d{1,3}\s*[-._)]?\s*", "", stem)


def _track_terms(track_title: str) -> list[str]:
    n_title = _normalize(track_title)
    short = _normalize(re.sub(r"[\(\[].*?[\)\]]", "", track_title))
    return [token for token in set((n_title + " " + short).split()) if token]


def _score_match(
    track_title: str,
    track_artist: str,
    stem_norm: str,
    same_title_count: int | None = None,
) -> int:
    n_title = _normalize(track_title)
    n_title_short = _normalize(re.sub(r"[\(\[].*?[\)\]]", "", track_title))
    n_artist = _normalize(track_artist)
    first_artist = _normalize(re.split(r"[,;&]| feat\.? | ft\.? | vs\.? | x ", track_artist)[0])
    score = 0
    if n_title and stem_norm == n_title:
        score += 5
    elif n_title and n_title in stem_norm:
        score += 3
    elif n_title_short and len(n_title_short) >= 4 and n_title_short in stem_norm:
        score += 2
    elif stem_norm in n_title:
        score += 1
    if score == 0:
        return 0
    if n_artist and n_artist in stem_norm:
        score += 3
    elif first_artist and first_artist in stem_norm:
        score += 2
    if score >= 5:
        return score
    return score if same_title_count == 1 else 0


def match_file(track_title: str, track_artist: str, files: list) -> Path | None:
    clean = [Path(f) for f in files if not is_partial_path(f)]
    stems = [(f, _normalize(_strip_track_number(f.stem))) for f in clean]
    n_title = _normalize(track_title)
    same_count = sum(1 for _f, stem in stems if n_title and n_title in stem)
    best, best_score = None, 0
    for f, stem in stems:
        if not stem:
            continue
        score = _score_match(track_title, track_artist, stem, same_count)
        if score > best_score:
            best, best_score = f, score
    return best if best_score >= 3 else None


@dataclass
class _IndexedFile:
    path: Path
    resolved: Path
    norm_stem: str


class LibraryIndex:
    def __init__(self, pl_dir: Path, sidecar_tracks: dict | None = None):
        self.pl_dir = Path(pl_dir)
        self.files: list[_IndexedFile] = []
        self.sidecar_identity = dict(sidecar_tracks or {})
        self.by_token: dict[str, list[int]] = {}
        self.enumerations = 0
        self.normalized_stems = 0
        self.candidate_checks = 0
        if not self.pl_dir.exists():
            return
        self.enumerations = 1
        for path in self.pl_dir.iterdir():
            if path.suffix.lower() not in AUDIO_EXTS or is_partial_path(path):
                continue
            norm = _normalize(_strip_track_number(path.stem))
            item = _IndexedFile(path=path, resolved=path.resolve(), norm_stem=norm)
            idx = len(self.files)
            self.files.append(item)
            self.normalized_stems += 1
            for token in set(norm.split()):
                self.by_token.setdefault(token, []).append(idx)

    def _same_title_count(self, title: str) -> int:
        n_title = _normalize(title)
        if not n_title:
            return 0
        terms = _track_terms(title)
        buckets = [self.by_token[token] for token in terms if token in self.by_token]
        indexes = set(min(buckets, key=len)) if buckets else range(len(self.files))
        return sum(1 for idx in indexes if n_title in self.files[idx].norm_stem)

    def mark_adopted(self, key: str, entry: dict) -> None:
        self.sidecar_identity[key] = dict(entry)

    def _bucket_intersection(self, tokens: list[str]) -> set[int]:
        buckets = [set(self.by_token[token]) for token in tokens if token in self.by_token]
        if not buckets:
            return set()
        return set.intersection(*buckets)

    def _candidate_indexes(self, title: str, artist: str = "") -> list[int]:
        full_tokens = _track_terms(title)
        short = _normalize(re.sub(r"[\(\[].*?[\)\]]", "", title))
        short_tokens = [token for token in short.split() if token]
        title_indexes = self._bucket_intersection(full_tokens)
        if not title_indexes:
            title_indexes = self._bucket_intersection(short_tokens)
        if not title_indexes:
            title_buckets = [self.by_token[token] for token in full_tokens + short_tokens if token in self.by_token]
            if not title_buckets:
                return []
            title_indexes = set(min(title_buckets, key=len))
        artist_tokens = _normalize(artist).split()
        artist_indexes = self._bucket_intersection(artist_tokens)
        if not artist_indexes and artist_tokens:
            artist_buckets = [self.by_token[token] for token in artist_tokens if token in self.by_token]
            artist_indexes = set().union(*(set(bucket) for bucket in artist_buckets)) if artist_buckets else set()
        if artist_indexes:
            narrowed = title_indexes & artist_indexes
            if narrowed:
                return sorted(narrowed)
        return sorted(title_indexes)

    def find(self, track_title: str, track_artist: str, used_files: set[Path]) -> Path | None:
        best, best_score = None, 0
        candidates = self._candidate_indexes(track_title, track_artist)
        same_title_count = self._same_title_count(track_title)
        for idx in candidates:
            item = self.files[idx]
            if item.resolved in used_files:
                continue
            self.candidate_checks += 1
            score = _score_match(track_title, track_artist, item.norm_stem, same_title_count)
            if score > best_score:
                best, best_score = item.path, score
        return best if best_score >= 3 else None

    def counters(self) -> dict[str, int]:
        return {
            "enumerations": self.enumerations,
            "normalized_stems": self.normalized_stems,
            "candidate_checks": self.candidate_checks,
        }


def _entry_for_track(sidecar_tracks: dict, t: dict) -> tuple[str, dict | None]:
    provider = t.get("provider", "deezer")
    key = track_key(t["id"], provider)
    entry = sidecar_tracks.get(key)
    if entry is None and provider == "deezer":
        entry = sidecar_tracks.get(str(t["id"]))
    return key, entry


def scan_playlist(pl_dir: Path, deezer_tracks: list) -> list:
    global _last_scan_counters
    lock_context = file_lock(sidecar_path(pl_dir)) if Path(pl_dir).exists() or sidecar_path(pl_dir).exists() else nullcontext()
    with lock_context:
        sc = load_sidecar(pl_dir)
        sidecar_tracks = sc.get("tracks", {})
        changed = False
        result = []
        index = LibraryIndex(pl_dir, sidecar_tracks)
        used_files: set[Path] = set()

        for t in deezer_tracks:
            key, entry = _entry_for_track(sidecar_tracks, t)
            if entry:
                fname = entry.get("file", "")
                f = Path(pl_dir) / fname
                if entry.get("status", "").startswith("verify_failed"):
                    status, err = "error", entry.get("error", "")
                elif is_ready_entry(pl_dir, entry):
                    status, err = "ok", ""
                    used_files.add(f.resolve())
                else:
                    status, err, fname = "missing", "", ""
                    entry["status"] = "missing"
                    entry["file"] = ""
                    changed = True
                fmt = entry.get("format", "")
            else:
                found = index.find(t["title"], t["artist"], used_files)
                if found:
                    status, err = "ok", ""
                    fmt = found.suffix.lstrip(".").lower()
                    fname = found.name
                    used_files.add(found.resolve())
                    adopted_entry = {
                        "title": t["title"],
                        "artist": t["artist"],
                        "file": fname,
                        "format": fmt,
                        "status": "ok",
                        "downloaded_at": int(time.time()),
                        "adopted": True,
                        "provider": t.get("provider", "deezer"),
                    }
                    sidecar_tracks[key] = adopted_entry
                    index.mark_adopted(key, adopted_entry)
                    changed = True
                else:
                    status, err, fmt, fname = "missing", "", "", ""
            result.append(
                {
                    **t,
                    "status": status,
                    "error": err,
                    "format": fmt,
                    "file": fname,
                    "flipped": bool(entry and entry.get("flipped_to") == "wav"),
                    "mp3_source": bool(entry and entry.get("mp3_source")),
                }
            )

        if changed:
            save_sidecar(pl_dir, sc)
        _last_scan_counters = index.counters()
        return result
