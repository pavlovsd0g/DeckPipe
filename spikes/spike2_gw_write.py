# -*- coding: utf-8 -*-
"""Спайк 2: запись в Deezer через GW API — создать плейлист, добавить трек, удалить трек, удалить плейлист."""
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
ARL = json.loads((ROOT / "config.local.json").read_text())["arl"]
TEST_TITLE = "DeckPipe test (delete me)"
TEST_TRACK = "3135556"  # Daft Punk - HBFS


class GW:
    def __init__(self, arl: str):
        self.s = requests.Session()
        self.s.cookies.set("arl", arl, domain=".deezer.com")
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        ud = self.call("deezer.getUserData", token="null")
        self.token = ud["checkForm"]
        self.user = ud["USER"]

    def call(self, method: str, token: str = None, **params):
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


def main():
    gw = GW(ARL)
    uid = gw.user["USER_ID"]
    print(f"OK: логин (user_id={uid})")

    # --- 1. Создать плейлист ---
    res = gw.call("playlist.create", TITLE=TEST_TITLE, STATUS=1, DESCRIPTION="spike2")
    print("playlist.create ->", json.dumps(res, ensure_ascii=False)[:200])
    pl_id = str(res if isinstance(res, (int, str)) else res.get("PLAYLIST_ID") or res.get("id"))
    assert pl_id and pl_id != "None", f"не получили id: {res}"
    print(f"OK: плейлист создан, id={pl_id}")

    try:
        # --- 2. Добавить трек ---
        res = gw.call("playlist.addSongs", PLAYLIST_ID=pl_id, OFFSET=-1,
                      SONGS=[{"SNG_ID": TEST_TRACK}])
        print("playlist.addSongs ->", json.dumps(res, ensure_ascii=False)[:200])

        # --- 3. Проверить, что трек внутри ---
        time.sleep(2)
        pl = requests.get(f"https://api.deezer.com/playlist/{pl_id}", timeout=15).json()
        tracks = [t["id"] for t in pl.get("tracks", {}).get("data", [])]
        print(f"Проверка: в плейлисте «{pl.get('title')}» треков: {len(tracks)}, ids={tracks}")
        assert int(TEST_TRACK) in tracks, "трек не появился в плейлисте"
        print("OK: трек добавлен и виден")

        # --- 4. Удалить трек из плейлиста ---
        res = gw.call("playlist.deleteSongs", PLAYLIST_ID=pl_id, SONGS=[TEST_TRACK])
        print("playlist.deleteSongs ->", json.dumps(res, ensure_ascii=False)[:200])
        time.sleep(2)
        pl = requests.get(f"https://api.deezer.com/playlist/{pl_id}", timeout=15).json()
        tracks = [t["id"] for t in pl.get("tracks", {}).get("data", [])]
        assert int(TEST_TRACK) not in tracks, "трек не удалился"
        print("OK: трек удалён из плейлиста")
    finally:
        # --- 5. Удалить тестовый плейлист ---
        res = gw.call("playlist.delete", PLAYLIST_ID=pl_id)
        print("playlist.delete ->", json.dumps(res, ensure_ascii=False)[:200])

    print("\nСПАЙК 2: УСПЕХ — создание/наполнение/очистка/удаление плейлиста работают через GW")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nСПАЙК 2: ПРОВАЛ — {type(e).__name__}: {e}")
        sys.exit(1)
