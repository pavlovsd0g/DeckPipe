# -*- coding: utf-8 -*-
"""Спайк 0: чтение master.db Rekordbox 7.2.14 через pyrekordbox (только чтение)."""
import sys
from pathlib import Path
import pyrekordbox.config as _cfg
from pyrekordbox import Rekordbox6Database

# Обход бага автоопределения: на этой машине master.db лежит в Pioneer\rekordbox
# (не rekordbox6), из-за чего падает assert в _get_rb7_config.
DB_PATH = Path(r"C:\Users\Никита\AppData\Roaming\Pioneer\rekordbox\master.db")
_cfg.__config__["rekordbox7"] = {"db_path": DB_PATH}

def main():
    db = Rekordbox6Database(path=DB_PATH)  # ключ SQLCipher подставляется встроенный
    print("OK: master.db открыта\n")

    # Версия базы
    try:
        props = db.get_property()
        for p in props:
            print(f"DBID={p.DBID} DBVersion={p.DBVersion}")
    except Exception as e:
        print("get_property failed:", e)

    # Плейлисты
    playlists = list(db.get_playlist())
    print(f"\nПлейлистов/папок всего: {len(playlists)}")
    for p in playlists[:25]:
        kind = {0: "playlist", 1: "folder", 4: "smart"}.get(p.Attribute, str(p.Attribute))
        n = len(p.Songs) if p.Attribute == 0 else "-"
        print(f"  [{kind:8}] id={p.ID:<6} tracks={n!s:<5} {p.Name}")

    # Треки
    contents = list(db.get_content())
    print(f"\nТреков в коллекции: {len(contents)}")
    for c in contents[:5]:
        print(f"  id={c.ID} | {c.Artist.Name if c.Artist else '?'} - {c.Title} | {c.FileNameL}")

    # Кью: найдём трек с cue-точками
    cues = list(db.get_cue())
    print(f"\nCue-записей всего: {len(cues)}")
    if cues:
        by_track = {}
        for q in cues:
            by_track.setdefault(q.ContentID, []).append(q)
        demo_id = max(by_track, key=lambda k: len(by_track[k]))
        demo = by_track[demo_id]
        track = next(c for c in contents if c.ID == demo_id)
        print(f"Пример: трек id={demo_id} «{track.Title}» — {len(demo)} кью:")
        for q in demo[:10]:
            kind = "memory" if q.Kind == 0 else f"hot {q.Kind}"
            loop = f" loop_out={q.OutMsec}" if q.OutMsec and q.OutMsec > 0 else ""
            print(f"  {kind:8} in={q.InMsec} ms{loop} color={q.Color} comment={q.Comment!r}")

    print("\nСПАЙК 0: УСПЕХ — pyrekordbox читает master.db Rekordbox 7.2.14")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nСПАЙК 0: ПРОВАЛ — {type(e).__name__}: {e}")
        sys.exit(1)
