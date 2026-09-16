"""Cooperative cancellation scoped to one download worker, including its subprocesses."""
from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from .atomic_io import cleanup_owned_stages


class DownloadCancelled(Exception):
    """Cancellation is a terminal user action, never a retryable provider error."""


class DownloadCleanupFailed(RuntimeError):
    """An owned partial could not be removed; do not claim cancellation is clean."""


_control = ContextVar("deckpipe_download_control", default=None)
_ytdlp_process_lock = threading.RLock()


@contextmanager
def cancellation_scope(requested, commit_lock, register=None):
    token = _control.set((requested, commit_lock, register))
    try:
        yield
    finally:
        _control.reset(token)


def cancellation_requested() -> bool:
    control = _control.get()
    return control is not None and control[0]()


def check_cancelled() -> None:
    if cancellation_requested():
        raise DownloadCancelled()


def register_stage(path, *, prefix=False) -> None:
    """Persist ownership before a provider or encoder can create any stage bytes."""
    check_cancelled()
    control = _control.get()
    if control is not None and control[2] is not None:
        control[2](path, prefix=prefix)


def cleanup_download_stages(*paths) -> None:
    try:
        cleanup_owned_stages(*paths)
    except OSError:
        if cancellation_requested():
            raise DownloadCleanupFailed("Download stopped but temporary file cleanup failed") from None
        raise


@contextmanager
def publication_guard():
    """Order final-file publication against acceptance of a cancellation request."""
    control = _control.get()
    if control is None:
        yield
    else:
        with control[1]:
            check_cancelled()
            yield


def run_media_process(command):
    """Reap ffmpeg before allowing callers to remove its owned stage on Windows."""
    check_cancelled()
    if _control.get() is None:
        return subprocess.run(command, capture_output=True, text=True)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            while True:
                check_cancelled()
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    check_cancelled()
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise


def _owned_popen_type(base):
    class OwnedPopen(base):
        """Keep yt-dlp's encoding/frozen-runtime setup; watch only this child handle."""

        def __init__(self, *args, **kwargs):
            control = _control.get()
            self._deckpipe_finished = threading.Event()
            self._deckpipe_watcher = None
            super().__init__(*args, **kwargs)
            if control is not None:
                requested = control[0]

                def watch():
                    while not self._deckpipe_finished.wait(0.1):
                        if self.poll() is not None:
                            return
                        if requested():
                            # Never find processes by executable/name/PID list.
                            # This is the handle constructed inside this job.
                            try:
                                self.kill()
                                self.wait(timeout=5)
                            except (OSError, subprocess.TimeoutExpired):
                                # The owning call still waits/reaps. It cannot
                                # claim cancellation or clean files prematurely.
                                pass
                            return

                self._deckpipe_watcher = threading.Thread(target=watch, name="DeckPipe-download-child", daemon=True)
                try:
                    self._deckpipe_watcher.start()
                except BaseException:
                    self.kill(timeout=None)
                    raise

        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                self._deckpipe_finished.set()
                if self._deckpipe_watcher is not None:
                    self._deckpipe_watcher.join(timeout=1)

    return OwnedPopen


@contextmanager
def cancellable_ytdlp_processes():
    """Bridge the pinned yt-dlp remux and external/HLS subprocess boundaries.

    Its hooks fire only before/after these blocking calls. Subclassing their
    Popen preserves yt-dlp's Windows/PyInstaller setup and blocking/reaping API.
    Other threads have no download scope and their children remain untouched.
    """
    if _control.get() is None:
        yield
        return
    from yt_dlp.downloader import external
    from yt_dlp.postprocessor import ffmpeg

    with _ytdlp_process_lock:
        originals = {module: module.Popen for module in (external, ffmpeg)}
        try:
            for module, base in originals.items():
                module.Popen = _owned_popen_type(base)
            yield
        finally:
            for module, base in originals.items():
                module.Popen = base
