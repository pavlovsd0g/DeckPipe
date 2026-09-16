# -*- coding: utf-8 -*-
"""Конвертация в WAV (ffmpeg, без пережатия сэмплрейта)."""
from pathlib import Path

import imageio_ffmpeg

from .atomic_io import make_staged_path
from .download_control import check_cancelled, cleanup_download_stages as cleanup_owned_stages, register_stage, run_media_process

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def convert_to_wav(src: Path, bit_depth: int = 16) -> Path:
    """src -> src.wav (рядом). Возвращает путь к WAV. Бросает исключение при ошибке."""
    check_cancelled()
    dst = make_staged_path(src.with_suffix(".wav"))
    register_stage(dst)
    codec = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}.get(bit_depth, "pcm_s16le")
    try:
        r = run_media_process([FFMPEG, "-y", "-v", "error", "-i", str(src), "-c:a", codec, str(dst)])
        check_cancelled()
        if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
            raise RuntimeError(f"ffmpeg: {r.stderr.strip()[:200] or 'конвертация не удалась'}")
    except BaseException:
        cleanup_owned_stages(dst)
        raise
    return dst
