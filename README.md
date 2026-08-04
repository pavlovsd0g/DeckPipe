# DeckPipe

Синхронизация плейлистов Deezer и SoundCloud с локальной DJ-библиотекой и Rekordbox.

## Возможности

- Плейлисты **Deezer** (GraphQL API, свой аккаунт по ARL-токену): статусы треков (✔ есть / ✖ нет / ⚠ ошибка), скачивание выбранных или полный синк, FLAC с fallback на MP3_320.
- Плейлисты/страницы **SoundCloud** (yt-dlp): добавление по URL, те же статусы и синк.
- Локальная библиотека: корень + кастомная папка на плейлист (вкл. USB), нечёткое сопоставление уже лежащих файлов (номера треков, регистр, апострофы — не важны).
- Верификация каждого файла после скачивания (полное декодирование + длительность vs каталог), авто-ретраи, лист ошибок.

## Запуск (dev)

```bash
python -m venv .venv
.venv/Scripts/pip install fastapi "uvicorn[standard]" requests pycryptodome mutagen imageio-ffmpeg deezer-python-gql yt-dlp pyrekordbox
cp config.local.json.example config.local.json   # вставить свой ARL
npm run dev                                       # http://localhost:7100
```

## Дорожная карта

См. [PLAN.md](PLAN.md): логин через встроенный браузер, WAV-flipper для CDJ,
синк с Rekordbox (master.db / XML), багрепорты в Telegram, десктопная сборка Win/Mac.
