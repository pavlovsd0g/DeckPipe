# -*- coding: utf-8 -*-
"""Конвертация в WAV (ffmpeg, без пережатия сэмплрейта)."""
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def convert_to_wav(src: Path, bit_depth: int = 16) -> Path:
    """src -> src.wav (рядом). Возвращает путь к WAV. Бросает исключение при ошибке."""
    dst = src.with_suffix(".wav")
    codec = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}.get(bit_depth, "pcm_s16le")
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(src), "-c:a", codec, str(dst)],
        capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError(f"ffmpeg: {r.stderr.strip()[:200] or 'конвертация не удалась'}")
    return dst
