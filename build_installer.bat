@echo off
chcp 65001 >nul 2>&1
title Сборка установщика Yandex.Disk Backup
cd /d "%~dp0"

python build_installer.py %*
echo.
pause
