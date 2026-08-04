# -*- coding: utf-8 -*-
"""Спайк 2 (GraphQL): создать плейлист, добавить трек, удалить трек, удалить плейлист."""
import asyncio
import json
import sys
from pathlib import Path

import requests
from deezer_python_gql import DeezerGQLClient

ROOT = Path(__file__).parent
ARL = json.loads((ROOT / "config.local.json").read_text())["arl"]
TEST_TITLE = "DeckPipe gql test (delete me)"
TEST_TRACK = "3135556"


async def main():
    client = DeezerGQLClient(arl=ARL)
    me = await client.get_me()
    print(f"OK: логин GraphQL (id={me.id})")

    # 1. создать плейлист
    pl = await client.create_playlist(title=TEST_TITLE, is_private=False, is_collaborative=False)
    inner = getattr(pl, "playlist", pl)
    pid = getattr(inner, "id", None)
    print(f"create_playlist -> id={pid} ({type(pl).__name__})")
    assert pid, f"нет id в ответе: {pl}"

    try:
        # 2. добавить трек
        r = await client.add_tracks_to_playlist(playlist_id=str(pid), track_ids=[TEST_TRACK])
        print("add_tracks_to_playlist ->", type(r).__name__)

        # 3. проверить через публичный API
        await asyncio.sleep(2)
        data = requests.get(f"https://api.deezer.com/playlist/{pid}", timeout=15).json()
        tracks = [t["id"] for t in data.get("tracks", {}).get("data", [])]
        print(f"проверка: «{data.get('title')}» треки={tracks}")
        assert int(TEST_TRACK) in tracks, "трек не появился"
        print("OK: трек добавлен и виден")

        # 4. удалить трек
        await client.remove_tracks_from_playlist(playlist_id=str(pid), track_ids=[TEST_TRACK])
        await asyncio.sleep(2)
        data = requests.get(f"https://api.deezer.com/playlist/{pid}", timeout=15).json()
        tracks = [t["id"] for t in data.get("tracks", {}).get("data", [])]
        assert int(TEST_TRACK) not in tracks, "трек не удалился"
        print("OK: трек удалён из плейлиста")
    finally:
        # 5. удалить плейлист
        await client.delete_playlist(playlist_id=str(pid))
        print("OK: тестовый плейлист удалён")

    print("\nСПАЙК 2: УСПЕХ — GraphQL мутации плейлистов работают (create/add/remove/delete)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nСПАЙК 2: ПРОВАЛ — {type(e).__name__}: {e}")
        sys.exit(1)
