# -*- coding: utf-8 -*-
"""Rekordbox: чтение master.db и синк плейлистов (прямая запись с бэкапом)."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pyrekordbox.config as _cfg
from pyrekordbox import Rekordbox6Database


def db_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox" / "master.db"


def db_exists() -> bool:
    return db_path().exists()


def rb_running() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"],
                       capture_output=True, text=True)
    return "rekordbox.exe" in r.stdout.lower()


def open_db() -> Rekordbox6Database:
    p = db_path()
    _cfg.__config__["rekordbox7"] = {"db_path": p}
    return Rekordbox6Database(path=p)


def backup_db() -> Path:
    src = db_path()
    dst = src.with_name(f"master.db.deckpipe-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(src, dst)
    # ротация: держим последние 5
    backups = sorted(src.parent.glob("master.db.deckpipe-backup-*"))
    for old in backups[:-5]:
        old.unlink(missing_ok=True)
    return dst


def get_rb_playlists() -> list:
    db = open_db()
    try:
        return [{"id": str(p.ID), "name": p.Name, "count": len(p.Songs)}
                for p in db.get_playlist() if p.Attribute == 0]
    finally:
        db.close()


def _norm(s: str) -> str:
    import re
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", (s or "").lower())


def sync_playlist(pl_name: str, ordered_files: list, create_missing: bool = True) -> dict:
    """Синк локального плейлиста в Rekordbox.
    ordered_files: [Path] в желаемом порядке. Возвращает статистику."""
    if rb_running():
        raise RuntimeError("Rekordbox запущен — закройте его и повторите")
    backup = backup_db()
    db = open_db()
    try:
        # индекс коллекции по путям
        by_path = {}
        for c in db.get_content():
            if c.FolderPath:
                by_path[os.path.normcase(str(c.FolderPath))] = c

        # найти/создать плейлист
        target = None
        for p in db.get_playlist():
            if p.Attribute == 0 and _norm(p.Name) == _norm(pl_name):
                target = p
                break
        if target is None:
            if not create_missing:
                raise RuntimeError(f"плейлист «{pl_name}» не найден в Rekordbox")
            target = db.create_playlist(pl_name)

        existing = {str(s.ContentID) for s in db.get_playlist_songs(PlaylistID=target.ID)}
        added_content = added_to_pl = 0
        for i, f in enumerate(ordered_files, start=1):
            key = os.path.normcase(str(f))
            content = by_path.get(key)
            if content is None:
                if not f.exists():
                    continue
                content = db.add_content(f)
                by_path[key] = content
                added_content += 1
            if str(content.ID) not in existing:
                db.add_to_playlist(target, content, track_no=i)
                existing.add(str(content.ID))
                added_to_pl += 1
        db.commit()
        return {"ok": True, "backup": str(backup), "playlist": target.Name,
                "added_content": added_content, "added_to_playlist": added_to_pl,
                "note": "новые треки без анализа — откройте Rekordbox и проанализируйте их (правый клик → Analyze Tracks)"}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
