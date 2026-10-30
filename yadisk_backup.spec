# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: оконное приложение + multi-call воркеры.

Сборка (onedir, его же упаковывает Inno Setup):
    pip install -r requirements.txt pyinstaller
    pyinstaller --noconfirm --clean yadisk_backup.spec

Результат: dist/YandexDiskBackup/YandexDiskBackup.exe и папка _internal.
Один и тот же exe запускает GUI и воркеры: `YandexDiskBackup.exe main.py scan`.
"""

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

hidden_imports = [
    # uvicorn/fastapi
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'starlette',
    'starlette.routing',
    'starlette.responses',
    'starlette.middleware',
    'yadisk',
    # yadisk импортирует сессию динамически через __import__(..., level=1),
    # поэтому PyInstaller не видит её при статическом анализе. Без этих
    # модулей собранный exe падает: No module named 'yadisk.sessions'.
    'yadisk.sessions',
    'yadisk.sessions.requests_session',
    'requests',
    'dotenv',
    'tenacity',
    'tqdm',
    'pydantic',
    'sqlite3',
    # Окно и трей
    'webview',
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    'webview.platforms.mshtml',
    'webview.platforms.win32',
    'pystray',
    'pystray._win32',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'clr',
    'pythonnet',
    'pythonnet.runtime',
    # Внутренние модули проекта
    'paths',
    'db_migrations',
    'version',
    'diagnostics',
    'monitor',
    'launcher',
    'main',
    'downloader',
    'state',
    'utils',
    'dedupe',
    'organize',
    'unlim_schema',
    'unlim_backup',
    'unlim_direct',
    'unlim_download',
    'unlim_worker',
    'fetch_cookies',
    'extract_cookies',
    'report_status',
    'watch',
    'verify',
    'verify_local',
]

datas = [
    ('webui_assets', 'webui_assets'),
    ('assets', 'assets'),
    ('.env.example', '.'),
    # История изменений: раздел текущей версии кладётся в ZIP диагностики.
    ('CHANGELOG.md', '.'),
]

# DLL WebView2 и Python.Runtime.dll должны лежать внутри пакетов, иначе
# pythonnet не найдёт сборки .NET при старте.
datas += collect_data_files('webview')
datas += collect_data_files('pythonnet')

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YandexDiskBackup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='YandexDiskBackup',
)
