@echo off
chcp 65001 >nul 2>&1
title Сборка YandexDiskBackup.exe
cd /d "%~dp0"

echo ============================================================
echo   Сборка автономного YandexDiskBackup.exe для Windows
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден в системе.
    echo Установите Python 3.10+ с python.org и отметьте Add Python to PATH.
    pause
    exit /b 1
)

python build_exe.py

echo.
pause
