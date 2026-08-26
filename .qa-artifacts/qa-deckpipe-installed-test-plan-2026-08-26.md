# Тест-план: DeckPipe 0.5.0 (установленная desktop-сборка)

**Окружение**: `E:\DeckPipe\deckpipe.exe`, Windows, локальный Python sidecar на `127.0.0.1`
**Дата**: 2026-08-26
**Скоуп**: запуск, UI shell, read-only API, Deezer/SoundCloud listings, Unicode, Rekordbox status, производительность старта
**Не входит**: скачивание треков, создание/изменение удалённых плейлистов, запись в `master.db`, WAV-flip, отправка багрепорта

## A. Pre-flight

| # | Проверка | Ожидание |
|---|---|---|
| A1 | Запуск установленного EXE | окно открывается, sidecar стартует, нет второго зависшего процесса |
| A2 | `/api/version` | HTTP 200, версия соответствует установленной сборке |
| A3 | `/` | UTF-8 HTML, интерфейс загружается без белого экрана |
| A4 | `/api/config` | секреты не возвращаются клиенту; статусы входа отображаются без токенов |
| A5 | Неизвестный API route | корректный 404, не SPA и не 500 |

## B. Read-only core flows

| # | Проверка | Ожидание |
|---|---|---|
| B1 | Deezer playlists | список загружается либо показывается понятная ошибка/empty state |
| B2 | SoundCloud sources | список загружается без принудительного внешнего изменения данных |
| B3 | Playlist tracks | статус `ok/missing/error`, путь и метаданные отображаются корректно |
| B4 | Rekordbox status | база и running-state определяются без записи |
| B5 | Errors list | endpoint отвечает и не теряет локальные ошибки молча |

## C. UI/UX desktop

| # | Проверка | Ожидание |
|---|---|---|
| C1 | Главное окно 1320x840 | нет обрезанных кнопок, горизонтального скролла и наложений |
| C2 | Минимальное окно 1000x640 | основные controls остаются доступны |
| C3 | Tabs and selection | Deezer, SoundCloud, Search, Errors переключаются |
| C4 | Empty/loading/error states | каждый state понятен, нет пустой панели без объяснения |
| C5 | Keyboard/accessibility tree | интерактивные элементы доступны по Tab и имеют понятные имена |
| C6 | Cyrillic/Unicode | русские названия и спецсимволы отображаются без `�` и квадратов |

## D. Performance baseline

| # | Проверка | Ожидание |
|---|---|---|
| D1 | EXE → окно | измерено, без субъективной оценки |
| D2 | API TTFB/size | измерены `/`, `/api/config`, `/api/playlists`, `/api/sc/sources`, `/api/rb/status` |
| D3 | Process footprint | измерены RAM/CPU GUI и sidecar после стабилизации |

## E. Safety and cleanup

| # | Проверка | Ожидание |
|---|---|---|
| E1 | Config hash before/after | неожиданные локальные мутации зафиксированы |
| E2 | External writes | 0 скачиваний, 0 remote playlist mutations, 0 Rekordbox writes |
| E3 | Cleanup | DeckPipe и sidecar закрыты; тестовых сущностей и creds-файлов не создавалось |

## Результат автоматизированного выполнения

Дата выполнения: 2026-08-26. Установленная версия: `0.5.0`.

| Gate | Итог | Evidence |
|---|---|---|
| A1–A5 | PASS | startup 3.31 s; 9/9 allowlisted GET contracts; unknown route = 404 |
| B1–B4 | PASS | live listings/status + isolated B3 `ok/missing/error` and UTF-8 sidecar |
| B5 | FAIL | local playlist errors отсутствуют в global errors; regression test = expected failure |
| C1 | PASS | все 6 обязательных controls видимы при 1320×840 |
| C2 | FAIL | при 1000×640 видимы 5 из 6 обязательных controls |
| C3–C4 | PASS (isolated) | 4/4 вкладки активируются; 4/4 explanatory states найдены |
| C5 | FAIL | 38 playlist titles найдены, keyboard-accessible = 0 |
| C6 | FAIL (data) | в копии Rekordbox: 23 bad Title, 22 bad Artist, 212 × U+FFFD |
| D1–D3 | PASS | startup/API/RAM/idle CPU в budget |
| PERF-01 | FAIL | 200×1000 cold scan 20.17–22.05 s против 36.6 ms warm sidecar |
| E1–E2 | PASS | user config, `master.db` и backup count неизменны; live mutations = 0 |
| E3 | FAIL | backend переживает graceful close; runner force-stops только install-owned PID, порт закрыт |

Основные артефакты:

- `.bench/runs/installed-20260826-181548.json`
- `.bench/runs/isolated-ui-20260826-182154.json`
- `.bench/runs/library-scan-20260826-182546.json`
- `.bench/runs/rekordbox-unicode-20260826-182719181.json`
- `.bench/baseline-installed-0.5.0.json`
- `audit/deckpipe-qa-performance-report-2026-08-26.html`

Authentication credentials, история Kimi, Telegram-реквизиты и token rotation в этот прогон не входили и не изменялись.
