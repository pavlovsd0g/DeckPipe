# -*- coding: utf-8 -*-
"""Спайк 1b: прямое скачивание Deezer FLAC — GW данные + классический encrypted URL + blowfish."""
import functools
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import requests
import imageio_ffmpeg
from Crypto.Cipher import AES, Blowfish
from mutagen import File as MutagenFile
from deezspot.deezloader import DeeLogin
from deezspot.deezloader.deegw_api import API_GW

ROOT = Path(__file__).parent
OUT = ROOT / "downloads"
OUT.mkdir(exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ARL = json.loads((ROOT / "config.local.json").read_text())["arl"]

BLOWFISH_SECRET = "g4el58wc0zvf9na1"
QUALITY_FORMAT_ID = {"FLAC": "9", "MP3_320": "3", "MP3_128": "1"}
QUALITY_FILESIZE_KEY = {"FLAC": "FILESIZE_FLAC", "MP3_320": "FILESIZE_MP3_320", "MP3_128": "FILESIZE_MP3_128"}


def blowfish_key(track_id: str) -> bytes:
    md5_hash = hashlib.md5(track_id.encode()).hexdigest()
    return "".join(
        chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
        for t in zip(md5_hash[:16], md5_hash[16:], BLOWFISH_SECRET)
    ).encode()


def decrypt_chunk(key: bytes, data: bytes) -> bytes:
    return Blowfish.new(key, Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07").decrypt(data)


def build_url(track_id: str, md5_origin: str, media_version: str, quality: str) -> str:
    to_encrypt = f"{md5_origin}{QUALITY_FORMAT_ID[quality]}{media_version}{track_id}"
    padding = 16 - (len(to_encrypt) % 16)
    to_encrypt += chr(padding) * padding
    encrypted = AES.new(b"jo6aey6haid2Teih", AES.MODE_ECB).encrypt(to_encrypt.encode())
    return f"https://e-cdns-proxy-{md5_origin[0]}.dzcdn.net/mobile/1/{encrypted.hex()}"


def download_track(track_id: str, quality: str = "FLAC") -> Path:
    infos = API_GW.get_song_data(track_id)
    title, artist = infos["SNG_TITLE"], infos["ART_NAME"]
    md5_origin = infos.get("MD5_ORIGIN", "")
    media_version = str(infos.get("MEDIA_VERSION", "1") or "1")

    # fallback по качеству: FLAC -> MP3_320 -> MP3_128
    for q in [quality, "MP3_320", "MP3_128"]:
        if int(infos.get(QUALITY_FILESIZE_KEY[q], "0") or 0) == 0:
            print(f"  {q}: недоступно (FILESIZE=0), пропуск")
            continue
        url = build_url(track_id, md5_origin, media_version, q)
        ext = "flac" if q == "FLAC" else "mp3"
        fpath = OUT / f"{artist} - {title}.{ext}"
        print(f"  качаю {q}: {artist} - {title}")
        key = blowfish_key(track_id)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            block_index = 0
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=2048):
                    if not chunk:
                        continue
                    if block_index % 3 == 0 and len(chunk) == 2048:
                        chunk = decrypt_chunk(key, chunk)
                    f.write(chunk)
                    block_index += 1
        # быстрая проверка: mutagen должен прочитать файл
        mf = MutagenFile(str(fpath))
        if mf is None:
            print(f"  {q}: файл не читается, пробую ниже")
            fpath.unlink()
            continue
        print(f"  OK: {fpath.name} ({fpath.stat().st_size/1e6:.1f} MB, {mf.info.length:.0f} c)")
        return fpath, q, infos
    raise RuntimeError("Не удалось скачать ни в одном качестве")


def main():
    dl = DeeLogin(arl=ARL)  # инициализация API_GW (логин по ARL)
    print("OK: логин ARL")

    track_id = "3135556"  # Daft Punk - HBFS
    fpath, q, infos = download_track(track_id, "FLAC")

    # --- верификация ---
    size = fpath.stat().st_size
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(fpath), "-f", "null", "-"],
                       capture_output=True, text=True)
    decode_ok = r.returncode == 0 and not r.stderr.strip()
    mf = MutagenFile(str(fpath))
    actual = float(mf.info.length)
    expected = int(infos["DURATION"])
    print(f"\nВерификация: размер={size/1e6:.1f}MB декод={'OK' if decode_ok else 'FAIL'} "
          f"длит.={actual:.1f}c (ожидалось ~{expected}c, delta={abs(actual-expected):.1f})")
    ok = size > 1e6 and decode_ok and abs(actual - expected) <= 2
    print("\nСПАЙК 1b:", f"УСПЕХ — скачан {q}, верификация пройдена" if ok else "ПРОВАЛ")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nСПАЙК 1b: ПРОВАЛ — {type(e).__name__}: {e}")
        sys.exit(1)
