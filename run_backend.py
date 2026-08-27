# -*- coding: utf-8 -*-
"""Backend entrypoint for the Tauri sidecar."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
import socket
import sys
import threading
import time
from typing import Callable, Protocol

import uvicorn

from app.security import SecuritySettings


class ParentHandle(Protocol):
    def wait_until_exit(self) -> None:
        ...

    def close(self) -> None:
        ...


SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF


def _socket_family(host: str) -> socket.AddressFamily:
    return socket.AF_INET6 if host == "::1" else socket.AF_INET


def _set_exclusive_address_use(listener: socket.socket) -> None:
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


def create_bound_listener(host: str, port: int) -> socket.socket:
    SecuritySettings.for_launch(host, 1, "listener-validation-token")
    listener = socket.socket(_socket_family(host), socket.SOCK_STREAM)
    try:
        _set_exclusive_address_use(listener)
        listener.bind((host, int(port)))
        listener.listen()
        return listener
    except Exception:
        listener.close()
        raise


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class _WindowsParentHandle:
    def __init__(self, handle: wintypes.HANDLE) -> None:
        self._handle = handle

    def wait_until_exit(self) -> None:
        result = _kernel32().WaitForSingleObject(self._handle, INFINITE)
        if result == WAIT_FAILED:
            raise OSError("parent wait failed")

    def close(self) -> None:
        _kernel32().CloseHandle(self._handle)


def _open_windows_parent(pid: int) -> ParentHandle | None:
    if pid <= 0 or os.name != "nt":
        return None
    handle = _kernel32().OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return None
    return _WindowsParentHandle(handle)


class _PollingParentHandle:
    def __init__(self, pid: int, interval: float = 0.5) -> None:
        self._pid = pid
        self._interval = interval

    def wait_until_exit(self) -> None:
        while True:
            try:
                os.kill(self._pid, 0)
            except OSError:
                return
            time.sleep(self._interval)

    def close(self) -> None:
        return None


def _open_parent_process(pid: int) -> ParentHandle | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _open_windows_parent(pid)
    return _PollingParentHandle(pid)


def start_parent_watchdog(
    server,
    parent_pid: int,
    open_parent: Callable[[int], ParentHandle | None] = _open_parent_process,
) -> threading.Thread | None:
    handle = open_parent(int(parent_pid))
    if handle is None:
        server.should_exit = True
        return None

    def watch_parent() -> None:
        try:
            handle.wait_until_exit()
        finally:
            handle.close()
            server.should_exit = True

    thread = threading.Thread(target=watch_parent, name="deckpipe-parent-watchdog", daemon=True)
    thread.start()
    return thread


def _parent_pid_from_env() -> int:
    value = os.environ.get("DECKPIPE_PARENT_PID")
    if not value:
        raise RuntimeError("DECKPIPE_PARENT_PID is required")
    try:
        pid = int(value)
    except ValueError as exc:
        raise RuntimeError("DECKPIPE_PARENT_PID must be an integer") from exc
    if pid <= 0:
        raise RuntimeError("DECKPIPE_PARENT_PID must be positive")
    return pid


def run_server_on_listener(
    listener: socket.socket,
    token: str,
    parent_pid: int,
    install_watchdog: bool = True,
) -> None:
    host, port = listener.getsockname()[:2]
    settings = SecuritySettings.for_launch(str(host), int(port), token)
    os.environ["DECKPIPE_API_TOKEN"] = settings.api_token
    os.environ["DECKPIPE_BOUND_HOST"] = settings.bound_host
    os.environ["DECKPIPE_BOUND_PORT"] = str(settings.bound_port)

    config = uvicorn.Config(
        "app.main:app",
        host=settings.bound_host,
        port=settings.bound_port,
        log_level="warning",
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    if install_watchdog:
        start_parent_watchdog(server, parent_pid)
    server.run(sockets=[listener])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    token = os.environ.get("DECKPIPE_API_TOKEN")
    if not token:
        raise RuntimeError("DECKPIPE_API_TOKEN is required")
    parent_pid = _parent_pid_from_env()
    listener = create_bound_listener(args.host, args.port)
    try:
        port = listener.getsockname()[1]
        SecuritySettings.for_launch(args.host, port, token)
        print(f"DECKPIPE_PORT={port}", flush=True)
        run_server_on_listener(listener, token, parent_pid)
    finally:
        listener.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
