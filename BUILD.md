# Сборка и проверка DeckPipe на Windows

Вход выполняется в отдельном окне DeckPipe без дополнений браузера. Профили Deezer и SoundCloud принадлежат DeckPipe: первый вход не наследует авторизацию обычного браузера, а «Выйти и забыть вход» очищает сессию выбранного сервиса. Статус и ограничения — в [плане релиза](docs/release-plan-2026-09-09.md).

Поддерживаемая цель — Windows x64, CPython 3.12, Rust/Tauri и Node.js. Используйте VS Build Tools с компонентами C++. Версии Python-пакетов и хеши зафиксированы в `requirements.lock` и `requirements-build.lock`, зависимости JS — в двух `package-lock.json`.

## Проверки исходников

В отдельном профиле приложения, без пользовательской музыки и авторизации:

```powershell
$env:DECKPIPE_DATA_DIR = 'D:\DeckPipe-RC-Lab\qa-evidence\developer-profile'
$env:DECKPIPE_API_TOKEN = 'synthetic-test-only'
$env:DECKPIPE_BOUND_PORT = '7100'
$env:PYTHONDONTWRITEBYTECODE = '1'
node frontend/build.mjs
& 'D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe' -m unittest discover -s qa/tests -p 'test_*.py'
```

Один тест сравнивает сгенерированные ресурсы с **HEAD** и `git archive HEAD`. После изменения frontend сначала соберите и закоммитьте `app/static` и `desktop/ui`; отличие незакоммиченных ресурсов от HEAD должно выявляться, а не скрываться.

PowerShell-проверки: `qa/tests/DeckPipe.QA.Tests.ps1`, `DeckPipe.Release.Tests.ps1`, `DeckPipe.Orchestrator.Tests.ps1`.

Для нативных проверок в текущей офлайн-лаборатории задайте все пути явно:

```powershell
$env:CARGO_HOME = 'D:\DeckPipe-RC-Lab\tool-cache\cargo-home'
$env:RUSTUP_HOME = 'D:\DeckPipe-RC-Lab\tool-cache\rustup-home'
$env:CARGO_TARGET_DIR = 'D:\DeckPipe-RC-Lab\build\auth-broker-target'
$env:DECKPIPE_AUTH_TEST_DIR = 'D:\DeckPipe-RC-Lab\qa-evidence\native-protocol'
cargo test --offline --locked --manifest-path desktop/src-tauri/Cargo.toml
```

Backend EXE остаётся единственным внешним sidecar; новому checkout сначала нужно подготовить его через release pipeline. Окно входа и его профили создаёт само приложение Tauri.

Для проверки **собранного EXE** изоляция отличается: frozen backend намеренно игнорирует `DECKPIPE_DATA_DIR`. Перед его запуском задайте дочернему процессу отдельные `APPDATA` и `LOCALAPPDATA` внутри новой папки лаборатории; профиль будет создан в `APPDATA/DeckPipe`. Не переносите туда рабочую конфигурацию или авторизацию. Тест исходников с `DECKPIPE_DATA_DIR` не доказывает изоляцию собранного приложения.

## Воспроизводимая сборка кандидата

Полный процесс: [release/README.md](release/README.md). Сборщик экспортирует закоммиченный HEAD в отдельную папку лаборатории, устанавливает зависимости из проверенного wheelhouse, собирает backend, интерфейс и установщики. Перед запуском должен быть закоммичен весь проверяемый исходный код.

```powershell
& ./release/build.ps1 `
  -StagingDirectory 'D:\DeckPipe-RC-Lab\staging\NEW-UNIQUE-CANDIDATE' `
  -PythonExe 'D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe' `
  -WheelhouseDirectory 'D:\DeckPipe-RC-Lab\wheelhouse' `
  -UnsignedEngineeringCandidate
```

Инженерный кандидат без подписи не является разрешением на распространение. Проверка должна показывать незакрытые условия подписи. Старый кандидат и его pin/evidence нельзя заменять результатом другой сборки. Для авторизованного private-beta применяется отдельный `-PrivateBetaCandidate` и существующая политика; публичный выпуск требует подписи и timestamp.

## Приёмка настоящей установки

Вход в реальные Deezer/SoundCloud, сохранение авторизации после перезапуска, загрузка и повторная синхронизация. Затем — установка/обновление/удаление приложения на чистой Windows и отдельная приёмка Rekordbox. Автоматические тесты используют синтетические данные и не закрывают эти сценарии.
