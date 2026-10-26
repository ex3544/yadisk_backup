@echo off
chcp 65001 >nul 2>&1
title Yandex.Disk Backup — Мастер установки
cd /d "%~dp0"
setlocal enabledelayedexpansion

cls
echo =====================================================================
echo           YANDEX.DISK BACKUP — ИНТЕРАКТИВНЫЙ МАСТЕР УСТАНОВКИ
echo =====================================================================
echo.
echo Этот мастер поможет проверить и установить все необходимые компоненты.
echo.

REM ────────────────── ШАГ 1: ПРОВЕРКА PYTHON ──────────────────
echo [1/4] Проверка Python в системе...
where python >nul 2>&1
if errorlevel 1 goto :no_python

python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo   ✓ Найдена подходящая версия: !PY_VER!
echo.
goto :check_pip

:no_python
echo   [!] Python не обнаружен в системе.
set /p ANS_PY="[?] Скачать и установить Python с официального сайта? (Д/Н / Y/N): "
if /i "!ANS_PY!"=="Д" goto :install_python
if /i "!ANS_PY!"=="Y" goto :install_python
if /i "!ANS_PY!"=="ДА" goto :install_python
if /i "!ANS_PY!"=="YES" goto :install_python
echo   [!] Установка Python пропущена. Без Python работа невозможна.
pause
exit /b 1

:old_python
echo   [!] Установленный Python слишком старый (требуется 3.11 или новее).
set /p ANS_PY="[?] Открыть страницу загрузки свежего Python 3.12? (Д/Н / Y/N): "
if /i "!ANS_PY!"=="Д" goto :install_python
if /i "!ANS_PY!"=="Y" goto :install_python
if /i "!ANS_PY!"=="ДА" goto :install_python
if /i "!ANS_PY!"=="YES" goto :install_python
pause
exit /b 1

:install_python
echo   → Пробую автоматическую установку через winget...
winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
if not errorlevel 1 (
    echo   ✓ Python успешно установлен через winget!
    echo   [!] Пожалуйста, перезапустите этот скрипт, чтобы обновились переменные PATH.
    pause
    exit /b 0
)
echo   → Открываю официальный сайт python.org...
echo   [ВАЖНО] При установке обязательно отметьте галочку "Add Python to PATH"!
start "" "https://www.python.org/downloads/"
echo   После завершения установки запустите этот файл снова.
pause
exit /b 0

REM ────────────────── ШАГ 2: ПРОВЕРКА PIP ──────────────────
:check_pip
echo [2/4] Проверка менеджера пакетов pip...
python -m pip --version >nul 2>&1
if not errorlevel 1 (
    echo   ✓ pip установлен и доступен.
    echo.
    goto :check_deps
)

echo   [!] Менеджер pip не найден.
set /p ANS_PIP="[?] Установить pip сейчас? (Д/Н / Y/N): "
if /i "!ANS_PIP!"=="Д" goto :install_pip
if /i "!ANS_PIP!"=="Y" goto :install_pip
if /i "!ANS_PIP!"=="ДА" goto :install_pip
if /i "!ANS_PIP!"=="YES" goto :install_pip
echo   [!] Без pip установка библиотек невозможна.
pause
exit /b 1

:install_pip
echo   → Установка pip...
python -m ensurepip --upgrade
if errorlevel 1 (
    echo   [!] Не удалось установить pip.
    pause
    exit /b 1
)
echo   ✓ pip успешно установлен!
echo.

REM ────────────────── ШАГ 3: ПРОВЕРКА ЗАВИСИМОСТЕЙ ───────────
:check_deps
echo [3/4] Проверка библиотек проекта...
python -c "import fastapi, uvicorn, yadisk, tqdm, dotenv, tenacity, requests" >nul 2>&1
if not errorlevel 1 (
    echo   ✓ Все библиотеки из requirements.txt уже установлены!
    echo.
    goto :check_build
)

echo   [!] Обнаружены отсутствующие библиотеки.
set /p ANS_DEP="[?] Установить необходимые библиотеки из requirements.txt? (Д/Н / Y/N): "
if /i "!ANS_DEP!"=="Д" goto :install_deps
if /i "!ANS_DEP!"=="Y" goto :install_deps
if /i "!ANS_DEP!"=="ДА" goto :install_deps
if /i "!ANS_DEP!"=="YES" goto :install_deps
echo   [!] Пропуск установки зависимостей.
echo.
goto :check_build

:install_deps
echo   → Установка библиотек через pip (1-2 мин)...
python -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1
python -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo   [!] Ошибка при установке зависимостей. Проверьте интернет.
    pause
    exit /b 1
)
echo   ✓ Все зависимости успешно установлены!
echo.

REM ────────────────── ШАГ 4: СБОРКА АВТОНОМНОГО EXE ──────────
:check_build
echo [4/4] Сборка автономного бинарника...
set /p ANS_EXE="[?] Собрать автономный YandexDiskBackup.exe (не требующий Python)? (Д/Н / Y/N): "
if /i "!ANS_EXE!"=="Д" goto :do_build
if /i "!ANS_EXE!"=="Y" goto :do_build
if /i "!ANS_EXE!"=="ДА" goto :do_build
if /i "!ANS_EXE!"=="YES" goto :do_build
goto :ask_launch

:do_build
echo   → Запуск сборщика PyInstaller...
python build_exe.py
echo.

REM ────────────────── ФИНАЛ: ЗАПУСК ───────────────────────────
:ask_launch
echo =====================================================================
echo   Все необходимые компоненты настроены!
echo =====================================================================
echo.
set /p ANS_RUN="[?] Запустить Web-интерфейс сейчас? (Д/Н / Y/N): "
if /i "!ANS_RUN!"=="Д" goto :launch
if /i "!ANS_RUN!"=="Y" goto :launch
if /i "!ANS_RUN!"=="ДА" goto :launch
if /i "!ANS_RUN!"=="YES" goto :launch
echo.
echo Для запуска в будущем используйте webui.bat или созданный dist\YandexDiskBackup.exe.
pause
exit /b 0

:launch
if exist "dist\YandexDiskBackup.exe" (
    echo Запуск автономного YandexDiskBackup.exe...
    start "" "dist\YandexDiskBackup.exe"
) else (
    echo Запуск через Python...
    start "" python webui.py
)
exit /b 0
