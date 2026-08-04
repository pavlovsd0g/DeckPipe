# Сборка DeckPipe

## Dev (веб-версия)

```bash
python -m venv .venv
.venv/Scripts/pip install fastapi "uvicorn[standard]" requests pycryptodome mutagen \
  imageio-ffmpeg deezer-python-gql yt-dlp pyrekordbox pillow
npm run dev    # http://localhost:7100
```

## Десктоп (Windows): Tauri + Python sidecar

Требования: Rust (rustup), VS Build Tools (C++ workload), Node.

```bash
# 1. бэкенд -> onefile exe
.venv/Scripts/python -m PyInstaller --noconfirm --onefile --name deckpipe-backend \
  --collect-all yt_dlp --collect-all deezer_python_gql --collect-all imageio_ffmpeg \
  --collect-all uvicorn --collect-all sqlcipher3 \
  --add-data "app/static;app/static" \
  --hidden-import pyrekordbox --hidden-import mutagen --hidden-import Crypto \
  --hidden-import app.main --hidden-import app.jobs --hidden-import app.library \
  --hidden-import app.deezer_client --hidden-import app.soundcloud --hidden-import app.tagger \
  --hidden-import app.converter --hidden-import app.bugreport --hidden-import app.rekordbox \
  --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on \
  run_backend.py
cp dist/deckpipe-backend.exe desktop/src-tauri/binaries/deckpipe-backend-x86_64-pc-windows-msvc.exe

# 2. Tauri
cd desktop
npm install
set CARGO_TARGET_DIR=D:\cargo-target\deckpipe   # без пробелов в пути!
npm run build
# готовое: %CARGO_TARGET_DIR%\release\bundle\nsis\DeckPipe_*_x64-setup.exe (+ MSI)
```

## macOS (когда понадобится)

Тот же пайплайн: PyInstaller `--onefile` на mac (tarball universal2 при желании),
имя sidecar — `deckpipe-backend-aarch64-apple-darwin`, `npm run build` соберёт `.app`/DMG.
Для распространения нужна нотаризация (Apple Developer, $99/год).

## Конфиг

- dev: `config.local.json` в корне проекта
- сборка: `%APPDATA%\DeckPipe\config.local.json` (Windows) / `~/Library/Application Support/DeckPipe` (mac)
