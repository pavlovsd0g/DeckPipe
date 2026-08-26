# -*- coding: utf-8 -*-
"""Точка входа бэкенда для PyInstaller-сборки (Tauri sidecar)."""
import argparse
import os
import socket
import sys

import uvicorn

from app.security import SecuritySettings


def _socket_family(host: str) -> socket.AddressFamily:
    return socket.AF_INET6 if host == "::1" else socket.AF_INET


def free_port(host: str) -> int:
    with socket.socket(_socket_family(host)) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _port_free(host: str, port: int) -> bool:
    try:
        with socket.socket(_socket_family(host)) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    token = os.environ.get("DECKPIPE_API_TOKEN")
    if not token:
        raise RuntimeError("DECKPIPE_API_TOKEN is required")
    try:
        SecuritySettings.for_launch(args.host, 1, token)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    port = args.port
    if port:
        if not _port_free(args.host, port):
            raise SystemExit(f"requested port is unavailable: {port}")
    else:
        port = free_port(args.host)
    SecuritySettings.for_launch(args.host, port, token)
    os.environ["DECKPIPE_BOUND_HOST"] = args.host
    os.environ["DECKPIPE_BOUND_PORT"] = str(port)
    # печатаем ТОЛЬКО финальный порт — Tauri читает его из stdout sidecar
    print(f"DECKPIPE_PORT={port}", flush=True)
    uvicorn.run("app.main:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
