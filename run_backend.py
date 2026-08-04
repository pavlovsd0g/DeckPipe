# -*- coding: utf-8 -*-
"""Точка входа бэкенда для PyInstaller-сборки (Tauri sidecar)."""
import argparse
import socket

import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    port = args.port or free_port()
    # порт печатаем — Tauri читает его из stdout sidecar
    print(f"DECKPIPE_PORT={port}", flush=True)
    uvicorn.run("app.main:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
