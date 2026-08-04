# -*- coding: utf-8 -*-
"""Спайк 1 (финал): ARL -> GW -> media.deezer.com -> FLAC/MP3 с blowfish-дешифровкой + верификация.

Рабочая цепочка (проверено 2026-07-31, аккаунт premium):
1. gw deezer.getUserData  -> checkForm (api_token) + USER.OPTIONS.license_token
2. gw song.getData        -> TRACK_TOKEN, DURATION, FILESIZE_FLAC/MP3_320
3. POST media.deezer.com/v1/get_url -> подписанный URL (cdnt-stream.dzcdn.net)
4. скачивание + Blowfish CBC stripe (каждый 3-й 2048-байтный блок)
"""
import functools
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import requests
import imageio_ffmpeg
from Crypto.Cipher import Blowfish
from mutagen.flac import FLAC as MutagenFLAC
from mutagen.mp3 import MP3 as MutagenMP3

ROOT = Path(__file__).parent
OUT = ROOT / "downloads"
OUT.mkdir(exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ARL = json.loads((ROOT / "config.local.json").read_text())["arl"]

BLOWFISH_SECRET = "g4el58wc0zvf9na1"
QUALITIES = ["FLAC", "MP3_320", "MP3_128"]
FILESIZE_KEY = {"FLAC": "FILESIZE_FLAC", "MP3_320": "FILESIZE_MP3_320", "MP3_128": "FILESIZE_MP3_128"}


class DeezerSession:
    """Минимальный клиент: ARL -> GW + media API."""

    def __init__(self, arl: str):
        self.s = requests.Session()
        self.s.cookies.set("arl", arl, domain=".deezer.com")
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        ud = self.gw("deezer.getUserData", token="null")
        self.token = ud["checkForm"]
        self.user = ud["USER"]
        self.license_token = self.user["OPTIONS"]["license_token"]

    def gw(self, method: str, token: str = None, **params):
        r = self.s.post(
            "https://www.deezer.com/ajax/gw-light.php",
            params={"api_version": "1.0", "api_token": token or self.token,
                    "method": method, "input": "3"},
            json=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"GW {method}: {data['error']}")
        return data["results"]

    def media_url(self, track_token: str, quality: str) -> str:
        order = [quality] + [q for q in QUALITIES if q != quality]
        payload = {"license_token": self.license_token,
                   "media": [{"type": "FULL",
                              "formats": [{"cipher": "BF_CBC_STRIPE", "format": q}
                                          for q in order]}],
                   "track_tokens": [track_token]}
        r = self.s.post("https://media.deezer.com/v1/get_url", json=payload, timeout=30)
        r.raise_for_status()
        media = r.json()["data"][0]["media"]
        for m in media:
            if m["format"] == quality:
                return m["sources"][0]["url"]
        raise RuntimeError(f"формат {quality} не выдан media API")


def blowfish_key(track_id: str) -> bytes:
    h = hashlib.md5(track_id.encode()).hexdigest()
    return "".join(
        chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
        for t in zip(h[:16], h[16:], BLOWFISH_SECRET)
    ).encode()


def download_track(ds: DeezerSession, track_id: str, prefer: str = "FLAC"):
    infos = ds.gw("song.getData", SNG_ID=track_id)
    title, artist = infos["SNG_TITLE"], infos["ART_NAME"]
    order = [prefer] + [q for q in QUALITIES if q != prefer]
    for q in order:
        if int(infos.get(FILESIZE_KEY[q], "0") or 0) == 0:
            print(f"  {q}: нет в каталоге, пропуск")
            continue
        url = ds.media_url(infos["TRACK_TOKEN"], q)
        ext = "flac" if q == "FLAC" else "mp3"
        fpath = OUT / f"{artist} - {title}.{ext}"
        key = blowfish_key(track_id)
        with ds.s.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            i = 0
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=2048):
                    if not chunk:
                        continue
                    if i % 3 == 0 and len(chunk) == 2048:
                        chunk = Blowfish.new(key, Blowfish.MODE_CBC,
                                             b"\x00\x01\x02\x03\x04\x05\x06\x07").decrypt(chunk)
                    f.write(chunk)
                    i += 1
        print(f"  скачан {q}: {fpath.name} ({fpath.stat().st_size/1e6:.1f} MB)")
        return fpath, q, infos
    raise RuntimeError("ни один формат недоступен")


def verify(fpath: Path, expected_duration: int) -> bool:
    mf = MutagenFLAC(str(fpath)) if fpath.suffix == ".flac" else MutagenMP3(str(fpath))
    actual = float(mf.info.length)
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(fpath), "-f", "null", "-"],
                       capture_output=True, text=True)
    decode_ok = r.returncode == 0 and not r.stderr.strip()
    ok = fpath.stat().st_size > 1e6 and decode_ok and abs(actual - expected_duration) <= 2
    print(f"  верификация: {actual:.1f}c (ожид. ~{expected_duration}c), "
          f"декод={'OK' if decode_ok else 'FAIL'} -> {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ds = DeezerSession(ARL)
    print(f"OK: логин ARL (user_id={ds.user['USER_ID']}, email={ds.user['EMAIL']})")

    # FLAC
    print("\n[1] FLAC-трек (3135556):")
    f1, q1, i1 = download_track(ds, "3135556", "FLAC")
    ok1 = verify(f1, int(i1["DURATION"]))

    # MP3-fallback: просим FLAC, но берём трек, где FLAC может отсутствовать,
    # плюс прямой запрос MP3_320 для проверки пути
    print("\n[2] MP3_320 напрямую (3135556):")
    f2, q2, i2 = download_track(ds, "3135556", "MP3_320")
    ok2 = verify(f2, int(i2["DURATION"]))

    ok = ok1 and ok2 and q1 == "FLAC" and q2 == "MP3_320"
    print("\nСПАЙК 1:", "УСПЕХ — FLAC и MP3_320 качаются, верификация пройдена" if ok else "ПРОВАЛ")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nСПАЙК 1: ПРОВАЛ — {type(e).__name__}: {e}")
        sys.exit(1)
