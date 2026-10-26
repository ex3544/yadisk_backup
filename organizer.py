#!/usr/bin/env python3
"""
organizer.py — Умный сортировщик файлов и медиа по категориям и датам (by PUH).
Разложит файлы по полочкам:
  - Фото / Видео / Документы / Архивы / Программы / Аудио / Прочее.
  - Внутри Фото и Видео — подпапки по годам и месяцам (Год/Месяц).
  - Дубликаты безопасно перемещает в папку duplicates/ на основе сверки размеров и хэшей SHA-256.
  - Включает встроенный веб-дашборд «все в одном окне» (по умолчанию порт 8002).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.server
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import paths

# Кодировка консоли
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Расширения категорий ────────────────────────────────────────────────────

CATEGORIES = {
    "Фото": {".jpg", ".jpeg", ".png", ".heic", ".raw", ".dng", ".webp", ".bmp", ".gif", ".tiff", ".cr2", ".nef", ".arw", ".svg"},
    "Видео": {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm", ".flv", ".wmv", ".ts", ".mts", ".m2ts", ".lrv"},
    "Документы": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv", ".md", ".epub", ".fb2", ".djvu", ".pages", ".numbers", ".key"},
    "Архивы": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso", ".cab", ".dmg"},
    "Программы": {".exe", ".msi", ".deb", ".rpm", ".apk", ".bat", ".cmd", ".sh", ".appimage", ".jar"},
    "Аудио": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".alac", ".opus", ".mid"},
}

EXT_TO_CATEGORY = {}
for cat, exts in CATEGORIES.items():
    for ext in exts:
        EXT_TO_CATEGORY[ext.lower()] = cat


# ── Извлечение даты из файла ────────────────────────────────────────────────

DATE_PATTERNS = [
    re.compile(r"(20\d\d|19\d\d)[-_.](\d{2})[-_.](\d{2})"),       # 2026-10-14, 2021_05_12
    re.compile(r"(20\d\d|19\d\d)(\d{2})(\d{2})"),                 # 20210512, 20190804
    re.compile(r"IMG[-_](20\d\d|19\d\d)(\d{2})(\d{2})", re.I),   # IMG_20210512
    re.compile(r"VID[-_](20\d\d|19\d\d)(\d{2})(\d{2})", re.I),   # VID_20210512
]


def extract_year_month(path: Path) -> tuple[str, str]:
    """Определяет год и месяц (YYYY, MM) по имени файла или дате создания."""
    name = path.name
    for pat in DATE_PATTERNS:
        m = pat.search(name)
        if m:
            year, month = m.group(1), m.group(2)
            try:
                y_int = int(year)
                m_int = int(month)
                if 1990 <= y_int <= 2035 and 1 <= m_int <= 12:
                    return f"{y_int:04d}", f"{m_int:02d}"
            except ValueError:
                pass

    # Fallback к mtime файла
    try:
        st = path.stat()
        dt = datetime.datetime.fromtimestamp(st.st_mtime)
        return f"{dt.year:04d}", f"{dt.month:02d}"
    except Exception:
        return "Неизвестно", "00"


# ── Хэширование для детекции дубликатов ──────────────────────────────────────


def quick_hash(path: Path, chunk_size: int = 65536) -> str:
    """Быстрый хэш первого блока файла."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read(chunk_size)
        h.update(data)
    return h.hexdigest()


def full_hash(path: Path) -> str:
    """Полный SHA-256 хэш файла."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── Движок сортировки ────────────────────────────────────────────────────────


class OrganizerEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.stop_requested = False
        self.progress = {
            "is_running": False,
            "total_files": 0,
            "processed": 0,
            "moved": 0,
            "duplicates": 0,
            "errors": 0,
            "current_file": "",
            "category_counts": {cat: 0 for cat in list(CATEGORIES.keys()) + ["Прочее", "Дубликаты"]},
            "logs": [],
            "start_time": 0,
            "elapsed_sec": 0,
        }

    def log(self, msg: str):
        print(msg)
        with self.lock:
            self.progress["logs"].append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")
            if len(self.progress["logs"]) > 200:
                self.progress["logs"].pop(0)

    def get_status(self) -> dict:
        with self.lock:
            p = dict(self.progress)
            if p["is_running"]:
                p["elapsed_sec"] = int(time.time() - p["start_time"])
            return p

    def stop(self):
        with self.lock:
            self.stop_requested = True
        self.log("[!] Получена команда остановки...")

    def run(self, src_dir_str: str, dst_dir_str: str, action: str = "move", dry_run: bool = False):
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            self.stop_requested = False
            self.progress = {
                "is_running": True,
                "total_files": 0,
                "processed": 0,
                "moved": 0,
                "duplicates": 0,
                "errors": 0,
                "current_file": "",
                "category_counts": {cat: 0 for cat in list(CATEGORIES.keys()) + ["Прочее", "Дубликаты"]},
                "logs": [],
                "start_time": time.time(),
                "elapsed_sec": 0,
            }

        threading.Thread(target=self._worker, args=(src_dir_str, dst_dir_str, action, dry_run), daemon=True).start()

    def _worker(self, src_dir_str: str, dst_dir_str: str, action: str, dry_run: bool):
        src_path = Path(src_dir_str)
        dst_path = Path(dst_dir_str)

        try:
            if not src_path.exists() or not src_path.is_dir():
                self.log(f"[-] Исходная папка не существует: {src_path}")
                return

            self.log(f"[*] Сканирование папки: {src_path}...")
            dst_path.mkdir(parents=True, exist_ok=True)

            # Собираем список всех файлов
            all_files: list[Path] = []
            for root, _, files in os.walk(src_path):
                # Не заходим в папку назначения если она внутри источника
                if Path(root).resolve() == dst_path.resolve() or dst_path.resolve() in Path(root).resolve().parents:
                    continue
                for f in files:
                    if f.endswith(".part") or f.startswith("."):
                        continue
                    all_files.append(Path(root) / f)

            with self.lock:
                self.progress["total_files"] = len(all_files)

            self.log(f"[+] Найдено файлов: {len(all_files)}. Анализ и группировка размеров...")

            # 1. Быстрая группировка по размеру для эффективного поиска дубликатов
            size_map: dict[int, list[Path]] = {}
            for p in all_files:
                try:
                    sz = p.stat().st_size
                    size_map.setdefault(sz, []).append(p)
                except Exception:
                    pass

            # База увиденных хэшей: hash -> original_dst_path
            seen_hashes: dict[str, Path] = {}
            dup_dir = dst_path / "duplicates"

            # 2. Основной цикл раскладки
            for i, file_path in enumerate(all_files, start=1):
                if self.stop_requested:
                    self.log("[!] Процесс сортировки прерван пользователем.")
                    break

                with self.lock:
                    self.progress["processed"] = i
                    self.progress["current_file"] = file_path.name

                try:
                    sz = file_path.stat().st_size
                    is_dup = False

                    # Проверка на дубликат если есть файлы такого же размера
                    if sz > 0 and len(size_map.get(sz, [])) > 1:
                        fh = full_hash(file_path)
                        if fh in seen_hashes:
                            is_dup = True
                        else:
                            seen_hashes[fh] = file_path
                    elif sz > 0:
                        # Уникальный размер, сразу фиксируем хэш при необходимости
                        pass

                    # Определение категории и целевой папки
                    if is_dup:
                        target_folder = dup_dir
                        cat_name = "Дубликаты"
                    else:
                        ext = file_path.suffix.lower()
                        cat_name = EXT_TO_CATEGORY.get(ext, "Прочее")
                        if cat_name in ("Фото", "Видео"):
                            year, month = extract_year_month(file_path)
                            target_folder = dst_path / cat_name / year / month
                        else:
                            target_folder = dst_path / cat_name

                    if not dry_run:
                        target_folder.mkdir(parents=True, exist_ok=True)
                        dest_file = target_folder / file_path.name

                        # Если файл с таким именем уже есть в целевой папке
                        if dest_file.exists() and dest_file.resolve() != file_path.resolve():
                            stem = file_path.stem
                            suffix = file_path.suffix
                            cnt = 1
                            while dest_file.exists():
                                dest_file = target_folder / f"{stem}_{cnt}{suffix}"
                                cnt += 1

                        if action == "move":
                            shutil.move(str(file_path), str(dest_file))
                        else:
                            shutil.copy2(str(file_path), str(dest_file))

                    with self.lock:
                        self.progress["moved"] += 1
                        if is_dup:
                            self.progress["duplicates"] += 1
                        self.progress["category_counts"][cat_name] += 1

                    if i % 100 == 0 or is_dup or i == len(all_files):
                        dup_str = " [ДУБЛИКАТ -> duplicates/]" if is_dup else ""
                        self.log(f"[{i}/{len(all_files)}] {file_path.name} -> {cat_name}{dup_str}")

                except Exception as e:
                    with self.lock:
                        self.progress["errors"] += 1
                    self.log(f"[-] Ошибка обработки {file_path.name}: {e}")

            self.log("[✓] Сортировка успешно завершена!")

        finally:
            with self.lock:
                self.progress["is_running"] = False
                self.is_running = False


engine = OrganizerEngine()


# ── Встроенный Веб-Интерфейс ─────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Органайзер медиа и файлов by PUH</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
      --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 24px;
    }
    .container { max-width: 1080px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .logo { font-size: 24px; font-weight: 700; color: var(--accent); display: flex; align-items: center; gap: 10px; }
    .badge {
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.3);
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 24px; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    .card-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
    .input-group { margin-bottom: 16px; }
    .input-group label { display: block; font-size: 13px; font-weight: 500; color: var(--text-muted); margin-bottom: 6px; }
    .input-group input, .input-group select {
      width: 100%;
      padding: 10px 14px;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-size: 14px;
    }
    .input-group input:focus, .input-group select:focus { outline: none; border-color: var(--accent); }
    .btn-row { display: flex; gap: 12px; margin-top: 10px; }
    button {
      flex: 1;
      padding: 12px 20px;
      font-size: 14px;
      font-weight: 600;
      border-radius: 8px;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: all 0.2s;
    }
    .btn-primary { background: var(--accent); color: #0f172a; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:hover { opacity: 0.9; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
    .stat-card {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      text-align: center;
    }
    .stat-val { font-size: 20px; font-weight: 700; color: var(--text); }
    .stat-lbl { font-size: 12px; color: var(--text-muted); }
    
    .cat-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
    .cat-chip {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 13px;
      display: flex;
      gap: 8px;
    }
    .cat-chip strong { color: var(--accent); }

    .progress-bar-container {
      width: 100%;
      height: 12px;
      background: #0f172a;
      border-radius: 999px;
      overflow: hidden;
      margin-bottom: 12px;
      border: 1px solid var(--border);
    }
    .progress-bar { height: 100%; width: 0%; background: var(--accent); transition: width 0.3s; }
    
    .log-box {
      background: #090d16;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: monospace;
      font-size: 12px;
      height: 220px;
      overflow-y: auto;
      color: #cbd5e1;
      white-space: pre-wrap;
    }
    footer { text-align: center; margin-top: 24px; color: var(--text-muted); font-size: 13px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo">
        <span>📁 Органайзер файлов и медиа</span>
      </div>
      <div class="badge">by PUH</div>
    </header>

    <div class="grid">
      <!-- Настройки запуска -->
      <div class="card">
        <div class="card-title">⚙️ Параметры раскладки</div>
        <div class="input-group">
          <label>Папка с файлами (Откуда сортировать):</label>
          <input type="text" id="srcDir" value="/media/vb/backup Ya1/Unlim">
        </div>
        <div class="input-group">
          <label>Целевая папка (Куда разложить):</label>
          <input type="text" id="dstDir" value="/media/vb/backup Ya1/Sorted">
        </div>
        <div class="input-group">
          <label>Действие над файлами:</label>
          <select id="action">
            <option value="move" selected>Переместить (быстро, экономит место на диске)</option>
            <option value="copy">Скопировать (сохранить исходники в папке)</option>
          </select>
        </div>
        <div class="input-group" style="display: flex; align-items: center; gap: 8px;">
          <input type="checkbox" id="dryRun" style="width: auto;">
          <label for="dryRun" style="margin: 0; cursor: pointer;">Тестовый запуск (Dry-Run без изменения файлов на диске)</label>
        </div>

        <div class="btn-row">
          <button id="startBtn" class="btn-primary" onclick="startOrganizer()">▶ Запустить сортировку</button>
          <button id="stopBtn" class="btn-danger" onclick="stopOrganizer()" disabled>⏹ Остановить</button>
        </div>
      </div>

      <!-- Прогресс и статус -->
      <div class="card">
        <div class="card-title">📊 Статус процесса</div>
        
        <div class="stats-grid">
          <div class="stat-card">
            <div class="stat-val" id="totalFiles">0</div>
            <div class="stat-lbl">Всего файлов</div>
          </div>
          <div class="stat-card">
            <div class="stat-val" id="processedFiles">0</div>
            <div class="stat-lbl">Обработано</div>
          </div>
          <div class="stat-card">
            <div class="stat-val" style="color: var(--success);" id="movedFiles">0</div>
            <div class="stat-lbl">Разложено</div>
          </div>
          <div class="stat-card">
            <div class="stat-val" style="color: var(--warning);" id="dupFiles">0</div>
            <div class="stat-lbl">Дубликатов</div>
          </div>
        </div>

        <div class="progress-bar-container">
          <div class="progress-bar" id="progressBar"></div>
        </div>
        <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px; display: flex; justify-content: space-between;">
          <span id="pctText">0%</span>
          <span id="elapsedText">0 сек</span>
        </div>

        <div class="card-title" style="font-size: 14px; margin-bottom: 8px;">Категории:</div>
        <div class="cat-chips" id="catChips">
          <!-- Заполняется динамически -->
        </div>
      </div>
    </div>

    <!-- Лог событий -->
    <div class="card">
      <div class="card-title">📜 Лог операций в реальном времени</div>
      <div class="log-box" id="logBox">Ожидание запуска...</div>
    </div>

    <footer>
      Органайзер медиа и файлов Яндекс.Диска • Разработано <strong>by PUH</strong>
    </footer>
  </div>

  <script>
    let pollTimer = null;

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderStatus(data);
      } catch (err) {
        console.error("Ошибка опроса статуса:", err);
      }
    }

    function renderStatus(p) {
      document.getElementById('totalFiles').textContent = p.total_files;
      document.getElementById('processedFiles').textContent = p.processed;
      document.getElementById('movedFiles').textContent = p.moved;
      document.getElementById('dupFiles').textContent = p.duplicates;

      const pct = p.total_files > 0 ? ((p.processed / p.total_files) * 100).toFixed(1) : 0;
      document.getElementById('progressBar').style.width = pct + '%';
      document.getElementById('pctText').textContent = pct + '% (' + p.processed + ' из ' + p.total_files + ')';
      document.getElementById('elapsedText').textContent = p.elapsed_sec + ' сек';

      document.getElementById('startBtn').disabled = p.is_running;
      document.getElementById('stopBtn').disabled = !p.is_running;

      // Чипсы категорий
      const chipsEl = document.getElementById('catChips');
      chipsEl.innerHTML = '';
      for (const [cat, cnt] of Object.entries(p.category_counts || {})) {
        if (cnt > 0) {
          const chip = document.createElement('div');
          chip.className = 'cat-chip';
          chip.innerHTML = cat + ': <strong>' + cnt + '</strong>';
          chipsEl.appendChild(chip);
        }
      }

      // Логи
      const logBox = document.getElementById('logBox');
      if (p.logs && p.logs.length > 0) {
        logBox.textContent = p.logs.join('\\n');
        logBox.scrollTop = logBox.scrollHeight;
      }
    }

    async function startOrganizer() {
      const src = document.getElementById('srcDir').value.trim();
      const dst = document.getElementById('dstDir').value.trim();
      const action = document.getElementById('action').value;
      const dryRun = document.getElementById('dryRun').checked;

      if (!src || !dst) {
        alert("Укажите исходную и целевую папки!");
        return;
      }

      try {
        await fetch('/api/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ src: src, dst: dst, action: action, dry_run: dryRun })
        });
        fetchStatus();
      } catch (err) {
        alert("Ошибка запуска: " + err);
      }
    }

    async function stopOrganizer() {
      try {
        await fetch('/api/stop', { method: 'POST' });
        fetchStatus();
      } catch (err) {
        alert("Ошибка остановки: " + err);
      }
    }

    // Запуск цикла опроса
    fetchStatus();
    setInterval(fetchStatus, 1000);
  </script>
</body>
</html>
"""

# Подставляем пути, актуальные для этой машины, вместо старых Linux-значений.
HTML_TEMPLATE = HTML_TEMPLATE.replace(
    "/media/vb/backup Ya1/Unlim", str(paths.DEFAULT_UNLIM_DIR)
).replace(
    "/media/vb/backup Ya1/Sorted", str(paths.DEFAULT_BACKUP_ROOT / "Sorted")
)


class OrganizerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif url.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            data = engine.get_status()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        if url.path == "/api/start":
            try:
                payload = json.loads(body.decode("utf-8"))
                src = payload.get("src", "")
                dst = payload.get("dst", "")
                act = payload.get("action", "move")
                dry = bool(payload.get("dry_run", False))
                engine.run(src, dst, action=act, dry_run=dry)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif url.path == "/api/stop":
            engine.stop()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Подавляем лишний вывод HTTP логов в терминал
        return


def run_web(host: str = "0.0.0.0", port: int = 8002):
    server = http.server.ThreadingHTTPServer((host, port), OrganizerHandler)
    print("=" * 70)
    print("  Органайзер файлов by PUH запущен!")
    print(f"  Веб-интерфейс доступен по адресу: http://127.0.0.1:{port}")
    print(f"  В сети / Tailscale               : http://{host}:{port}")
    print("=" * 70)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Остановка сервера...")
        server.server_close()


# ── Точка входа ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="organizer.py — Умный сортировщик файлов по папкам и датам с детекцией дубликатов (by PUH)"
    )
    parser.add_argument("--src", type=str, help="Папка с исходными файлами (например, выгруженный Безлимит)")
    parser.add_argument("--dst", type=str, help="Целевая папка для организованной структуры")
    parser.add_argument("--action", choices=["move", "copy"], default="move", help="Переместить (move) или скопировать (copy)")
    parser.add_argument("--dry-run", action="store_true", help="Тестовый запуск без изменения файлов на диске")
    parser.add_argument("--web", action="store_true", help="Запустить автономный веб-дашборд")
    parser.add_argument("--port", type=int, default=8002, help="Порт для веб-интерфейса (по умолчанию 8002)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Хост веб-интерфейса (по умолчанию 0.0.0.0)")

    args = parser.parse_args()

    # Если указан --web или запущен без обязательных папок --src и --dst, поднимаем Веб-дашборд
    if args.web or not (args.src and args.dst):
        run_web(host=args.host, port=args.port)
    else:
        # Консольный запуск
        engine.run(args.src, args.dst, action=args.action, dry_run=args.dry_run)
        while engine.is_running:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
