@echo off
REM Установка Visual Studio Build Tools (C++) для сборки DeckPipe (Tauri/Rust).
REM Запусти ПРАВОЙ кнопкой -> "Запуск от имени администратора".
echo Устанавливаю VS Build Tools (VCTools)... это займёт 10-20 минут и ~3 ГБ.
"C:\Users\peche\AppData\Local\Temp\vs_buildtools.exe" --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive
echo.
echo Готово. Теперь скажи Кими «продолжай сборку».
pause
