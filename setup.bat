@echo off
chcp 65001 >nul 2>&1
title Yandex.Disk Backup — Установка и запуск
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================================
echo   Yandex.Disk Backup — установка недостающего и запуск
echo ============================================================
echo.

REM ========== 1. Поиск рабочего Python 3.11+ ==========
call :find_python
if defined PYEXE (
    echo [OK] Найден Python:
    "!PYEXE!" --version
    echo.
    goto :install_deps
)

REM ========== 2. Установка Python через winget ==========
echo [!] Python 3.11+ не найден.
where winget >nul 2>&1
if errorlevel 1 goto :manual

echo [setup] Устанавливаю Python 3.13 через winget ^(1-2 мин^)...
winget install -e --id Python.Python.3.13 --silent --accept-source-agreements --accept-package-agreements --disable-interactivity
if errorlevel 1 goto :manual

call :find_python
if defined PYEXE (
    echo [OK] Python установлен:
    "!PYEXE!" --version
    echo.
    goto :install_deps
)

:manual
echo.
echo [!] Не удалось установить Python автоматически.
echo     Открываю официальный сайт python.org ...
start "" "https://www.python.org/downloads/"
echo     При установке ОБЯЗАТЕЛЬНО отметьте "Add Python to PATH".
echo     После установки запустите этот файл ещё раз.
pause
exit /b 1

REM ========== 3. Установка зависимостей ==========
:install_deps
echo [setup] Проверка и установка библиотек ^(первый раз 1-2 мин^)...
"!PYEXE!" -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1
"!PYEXE!" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo [!] Не удалось установить зависимости. Проверьте интернет.
    pause
    exit /b 1
)
echo [OK] Все зависимости установлены.
echo.

REM ========== 4. Запуск сервера ==========
echo [run] Запускаю веб-интерфейс: http://localhost:8000
echo.
"!PYEXE!" webui.py

echo.
echo ============================================================
echo   Сервер остановлен.
echo ============================================================
pause
exit /b 0

REM ========== Поиск рабочего Python 3.11+ ==========
:find_python
set "PYEXE="
for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PYEXE (
        echo %%~p | findstr /i /c:"WindowsApps" >nul
        if errorlevel 1 (
            "%%~p" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYEXE=%%~p"
        )
    )
)
if defined PYEXE goto :eof
for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%d\python.exe" (
        "%%d\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%d\python.exe"
    )
)
if defined PYEXE goto :eof
for /d %%d in ("%ProgramFiles%\Python\Python3*") do (
    if exist "%%d\python.exe" (
        "%%d\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%d\python.exe"
    )
)
goto :eof
