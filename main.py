"""CLI для бэкапа Яндекс.Диска.

Команды:
    python main.py scan                 # обход дерева, заполнить state без скачивания
    python main.py download             # скачать всё pending
    python main.py download --workers 4 # с заданным числом потоков
    python main.py retry-failed         # сбросить failed → pending и скачать
    python main.py stats                # статистика по БД
    python main.py verify               # проверить md5 локальных файлов
    python main.py check                # проверить токен и доступ к диску
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

# В установленной сборке stdout дочернего процесса — консольная кодировка
# Windows (cp1251), и любая печать «→» или кириллического имени падала с
# UnicodeEncodeError, обрывая сканирование. Остальные скрипты делают то же.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from tqdm import tqdm

import paths
import selection
import yadisk
from downloader import (
    check_token,
    download_all,
    scan_media,
    scan_tree,
    write_status_final,
    write_status_start,
)
from fetch import DeadHosts
from state import State
from utils import (
    auto_workers,
    compute_md5,
    file_size_safe,
    human_bytes,
    human_duration,
    normalize_scan_roots,
    normalize_unlim_media,
)

# Пути программы и данных — в paths.py, здесь только короткие псевдонимы.
PROJECT_DIR = paths.APP_DIR
LOG_DIR = paths.LOG_DIR
LOG_FILE = paths.BACKUP_LOG
DB_FILE = paths.DB_FILE


def setup_logging(level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_h = RotatingFileHandler(
        LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)

    # Консольный хендлер пишет только WARNING+ — чтобы не мешать tqdm-барам
    console_h = logging.StreamHandler(stream=sys.stderr)
    console_h.setLevel(logging.WARNING)
    console_h.setFormatter(fmt)

    root = logging.getLogger("yadisk_backup")
    root.setLevel(level.upper())
    # Сбрасываем хендлеры на случай повторного вызова
    root.handlers.clear()
    root.addHandler(file_h)
    root.addHandler(console_h)
    root.propagate = False


def load_config(require_token: bool = True) -> dict:
    """Грузит .env из директории проекта.

    Если require_token=True и токен не задан — выходим с понятной ошибкой.
    Для команд stats/verify токен не нужен, можно работать только с БД.
    """
    dotenv_path = paths.ENV_FILE
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
    elif require_token:
        sys.exit(
            f"Не найден файл {dotenv_path}.\n"
            f"Скопируй .env.example в .env и пропиши YA_DISK_TOKEN.\n"
            f"Токен взять тут: https://yandex.ru/dev/disk/poligon/"
        )

    token = os.getenv("YA_DISK_TOKEN", "").strip()
    download_dir = os.getenv("DOWNLOAD_DIR", str(paths.DEFAULT_DISK_DIR)).strip()
    try:
        max_workers = int(os.getenv("MAX_WORKERS", "4"))
    except ValueError:
        max_workers = 4
    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"

    # Превью отложенных фото: включены по умолчанию, выключаются PREVIEWS=0.
    previews = (os.getenv("PREVIEWS", "1") or "1").strip().lower() not in ("0", "false", "no")

    # Какие облачные папки обходить (C2). Пусто — весь Диск.
    scan_roots = normalize_scan_roots(os.getenv("DISK_SCAN_ROOTS", "/"))
    # Что качать у Безлимита по умолчанию: фото, видео или всё (C2).
    unlim_media = normalize_unlim_media(os.getenv("UNLIM_MEDIA", "all"), "all")

    return {
        "token": token,
        "download_dir": download_dir,
        "max_workers": max_workers,
        "log_level": log_level,
        "previews": previews,
        "scan_roots": scan_roots,
        "unlim_media": unlim_media,
    }


# ── команды ─────────────────────────────────────────────────────────────────


def cmd_check(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    print(msg)
    return 0 if ok else 2


def cmd_scan(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    state = State(str(DB_FILE))
    if getattr(args, "force", False):
        print("[!] Флаг --force: пересоздаём базу state.db заново с нуля...", flush=True)
        state.reset()
    client = yadisk.Client(token=cfg["token"])

    # Приоритет: явные флаги командной строки, затем выбор из настроек окна.
    if args.roots:
        roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    elif args.root and args.root != "/":
        roots = [args.root]
    else:
        roots = list(cfg.get("scan_roots") or ["/"])

    total_files = 0
    total_bytes = 0
    try:
        for root in roots:
            print(f"Обхожу {root}...")
            progress = tqdm(unit="file", desc=f"Найдено [{root}]", leave=False)

            def on_file(path: str, size: int) -> None:
                progress.update(1)

            files, bytes_ = scan_tree(
                client=client,
                state=state,
                root=root,
                on_file=on_file,
            )
            progress.close()
            print(f"  → {root}: {files} файлов, {human_bytes(bytes_)}")
            total_files += files
            total_bytes += bytes_
    finally:
        client.close()
        state.close()

    print(f"\nИтого: {total_files} файлов, {human_bytes(total_bytes)}")
    return 0


def cmd_scan_media(cfg: dict, args: argparse.Namespace) -> int:
    """Сканирует ТОЛЬКО фото и видео со всего диска (через media_type)."""
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    media_types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    print(f"Сканирую медиа: {', '.join(media_types)}")

    state = State(str(DB_FILE))
    client = yadisk.Client(token=cfg["token"])

    progress = tqdm(unit="file", desc="Найдено медиа")

    def on_file(path: str, size: int) -> None:
        progress.update(1)

    try:
        files, total_bytes = scan_media(
            client=client,
            state=state,
            media_types=media_types,
            on_file=on_file,
        )
    finally:
        progress.close()
        client.close()
        state.close()

    print(f"Найдено медиа-файлов: {files}")
    print(f"Суммарный объём: {human_bytes(total_bytes)}")
    return 0


def _download_selection(cfg: dict, args: argparse.Namespace, state: State) -> int:
    """Скачивает только отмеченное в окне («Что качать»).

    Статусы в базе не переписываются: невыбранное остаётся в очереди, а уже
    скачанное в задания не попадает (его нет среди pending/failed/lost).
    """
    # Сразу говорим окну, что это выгрузка выбранного: по этому флагу панель
    # покажет «Выбрано N, скачано X из N», а не «база готова».
    write_status_start("selection")
    client = yadisk.Client(token=cfg["token"])
    try:
        built = selection.build_disk_jobs(
            state, client, do_scan=True, log=logging.getLogger("yadisk_backup")
        )
    finally:
        client.close()

    jobs = built["jobs"]
    print(
        f"Выбор: папок {built['folders']}, файлов {built['files']}; "
        f"к скачиванию {len(jobs)}.",
        flush=True,
    )
    if built["already_done"]:
        print(f"Уже скачано, повторно не берём: {built['already_done']}.")
    if not jobs:
        # Статус уже помечен как «идёт» — закрываем его, иначе окно решит,
        # что выгрузка выбранного зависла.
        write_status_final("selection", "empty")
        state.close()
        return 0

    workers = args.workers or auto_workers()
    t0 = time.time()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}
    dead_hosts = DeadHosts()
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
            jobs=jobs,
            dead_hosts=dead_hosts,
            make_previews=cfg.get("previews", True),
            mode="selection",
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Прогресс сохранён в state.db.")
        return 130
    finally:
        elapsed = time.time() - t0
        _print_summary(counters, elapsed)
        state.close()
    return 0 if counters["failed"] == 0 else 1


def cmd_download(cfg: dict, args: argparse.Namespace) -> int:
    # Статус для Web UI появляется сразу: проверка токена и скан идут секунды,
    # и всё это время плашка не должна висеть в «подготовке».
    write_status_start()

    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    state = State(str(DB_FILE))

    # Сгоревшие файлы получают второй шанс в начале каждого запуска: те, что
    # прождали свою паузу (полчаса), возвращаются в очередь — но не больше 200
    # за запуск. В режиме «скачать выбранное» ничего не трогаем: выбор и так
    # берёт сгоревшие файлы внутри отмеченных папок.
    if not getattr(args, "selection", False) and not getattr(args, "no_return_lost", False):
        returned = state.return_lost()
        if returned:
            print(f"Сгоревших возвращено в очередь: {returned}", flush=True)

    # Режим «скачать выбранное»: список работ берётся из selection.json,
    # а не из всей очереди базы.
    if getattr(args, "selection", False):
        return _download_selection(cfg, args, state)

    pending_count = state.get_stats().get("pending", 0)

    # Если ничего pending — предложить сделать scan
    if pending_count == 0:
        print("В БД нет pending-файлов. Сначала выполни: python main.py scan")
        # Но всё-равно попробуем — может пользователь сначала запустил download
        if not args.no_scan:
            print("Запускаю scan автоматически (--no-scan чтобы отключить)...")
            client = yadisk.Client(token=cfg["token"])
            auto_roots = (
                [args.root] if args.root and args.root != "/"
                else list(cfg.get("scan_roots") or ["/"])
            )
            try:
                for auto_root in auto_roots:
                    if len(auto_roots) > 1:
                        print(f"Обхожу {auto_root}...")
                    scan_tree(client, state, root=auto_root)
            finally:
                client.close()

    workers = args.workers or auto_workers()
    print(f"Старт скачивания в {workers} потоков → {cfg['download_dir']}")

    t0 = time.time()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}
    # Общий реестр на оба прохода: мёртвый сервер не должен заново ждать
    # по 10 секунд на каждом файле во второй раз.
    dead_hosts = DeadHosts()
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
            dead_hosts=dead_hosts,
            make_previews=cfg.get("previews", True),
        )

        # Один повторный проход для сбойных файлов (по --retry).
        # Сбрасываем и старые failed, даже если в этом проходе новых падений не
        # было: иначе при пустой очереди pending галочка «повторить сбойные»
        # ничего не делала, хотя в базе оставались failed-файлы.
        if args.retry:
            n = state.reset_failed_to_pending()
            if n > 0:
                print(f"\n— Повторный проход для сбойных файлов ({n} → pending)…")
                # Передаём уже накопленные счётчики: итог сессии должен включать
                # оба прохода, иначе плашка показывала только второй.
                counters = download_all(
                    token=cfg["token"],
                    state=state,
                    download_dir=cfg["download_dir"],
                    workers=workers,
                    session=counters,
                    dead_hosts=dead_hosts,
                    make_previews=cfg.get("previews", True),
                )
            else:
                print("\n— Сбойных файлов нет, повтор не нужен.")
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Прогресс сохранён в state.db.")
        state.close()
        return 130
    finally:
        elapsed = time.time() - t0
        _print_summary(counters, elapsed)
        state.close()

    return 0 if counters["failed"] == 0 else 1


def cmd_retry_failed(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2

    state = State(str(DB_FILE))
    n = state.reset_failed_to_pending()
    print(f"Сброшено failed → pending: {n}")
    if n == 0:
        state.close()
        return 0

    workers = args.workers or auto_workers()
    t0 = time.time()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
            make_previews=cfg.get("previews", True),
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        state.close()
        return 130
    finally:
        elapsed = time.time() - t0
        _print_summary(counters, elapsed)
        state.close()

    return 0 if counters["failed"] == 0 else 1


def cmd_stats(cfg: dict, args: argparse.Namespace) -> int:
    state = State(str(DB_FILE))
    s = state.get_stats()
    state.close()

    print("Статистика по БД state.db:")
    print(f"  Всего файлов:        {s['total']}")
    print(f"    pending:           {s['pending']}")
    print(f"    downloaded:        {s['downloaded']}")
    print(f"    skipped:           {s['skipped']}")
    print(f"    failed:            {s['failed']}")
    if s.get("waiting"):
        print(f"    ждут Яндекса:      {s['waiting']} (входят в pending)")
    print(f"  Объём всего:         {human_bytes(s['bytes_total'])}")
    print(f"  Объём скачано/skip:  {human_bytes(s['bytes_downloaded'])}")
    return 0


def cmd_verify(cfg: dict, args: argparse.Namespace) -> int:
    """Проверяет MD5 локальных файлов против того что хранит API."""
    state = State(str(DB_FILE))
    rows = list(state.get_all_downloaded())
    if not rows:
        print("Нет скачанных файлов в БД для проверки.")
        state.close()
        return 0

    bad: list[tuple[str, str]] = []
    missing: list[str] = []
    size_mismatch: list[str] = []

    bar = tqdm(rows, desc="Проверка MD5", unit="file")
    for row in bar:
        remote = row["remote_path"]
        local = row["local_path"]
        expected_md5 = row["md5"]
        expected_size = row["size"]

        size = file_size_safe(local)
        if size is None:
            missing.append(remote)
            continue
        if expected_size is not None and expected_size != size:
            size_mismatch.append(f"{remote} (local={size}, remote={expected_size})")
            continue
        if not expected_md5:
            continue  # API не вернул MD5 — проверить нечем
        try:
            actual = compute_md5(local)
        except OSError as e:
            bad.append((remote, f"read error: {e}"))
            continue
        if actual.lower() != expected_md5.lower():
            bad.append((remote, f"md5 mismatch: local={actual}, remote={expected_md5}"))

    bar.close()
    state.close()

    print(f"\nПроверено: {len(rows)}")
    print(f"  Отсутствует локально:    {len(missing)}")
    print(f"  Размер не совпал:        {len(size_mismatch)}")
    print(f"  MD5 не совпал:           {len(bad)}")

    if missing:
        print("\nОтсутствующие файлы (первые 20):")
        for p in missing[:20]:
            print(f"  {p}")
    if size_mismatch:
        print("\nРасхождение по размеру (первые 20):")
        for p in size_mismatch[:20]:
            print(f"  {p}")
    if bad:
        print("\nРасхождение по MD5 (первые 20):")
        for p, msg in bad[:20]:
            print(f"  {p}: {msg}")

    return 0 if not (missing or size_mismatch or bad) else 1


def _print_summary(counters: dict, elapsed: float) -> None:
    line = (
        f"\nИтого: скачано {counters['downloaded']} ({human_bytes(counters['bytes'])}) | "
        f"пропущено {counters['skipped']} | упало {counters['failed']}"
    )
    if counters.get("deferred"):
        line += f" | ждут Яндекса {counters['deferred']}"
    print(line + f" | время {human_duration(elapsed)}")


# ── argparse ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yadisk_backup",
        description="Полный бэкап Яндекс.Диска через официальный REST API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Проверить токен и доступ к диску")
    p_check.set_defaults(func=cmd_check)

    p_scan = sub.add_parser("scan", help="Обойти дерево и заполнить БД (без скачивания)")
    p_scan.add_argument("--root", default="/", help="Корневой путь обхода (по умолчанию /)")
    p_scan.add_argument(
        "--roots",
        default=None,
        help="Несколько корней через запятую, напр.: /Фотокамера,/Видео,/Скриншоты",
    )
    p_scan.add_argument(
        "--force",
        action="store_true",
        help="Пересоздать базу с нуля: все статусы скачанного теряются",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_scan_media = sub.add_parser(
        "scan-media",
        help="Сканировать только фото и видео (включая безлимитное фото-хранилище)",
    )
    p_scan_media.add_argument(
        "--types",
        default="image,video",
        help="Типы медиа через запятую: image,video (по умолчанию)",
    )
    p_scan_media.set_defaults(func=cmd_scan_media)

    p_dl = sub.add_parser("download", help="Скачать все pending файлы")
    p_dl.add_argument("--workers", type=int, default=None, help="Число потоков (макс 5)")
    p_dl.add_argument("--root", default="/", help="Корень для авто-scan если БД пуста")
    p_dl.add_argument("--no-scan", action="store_true", help="Не делать авто-scan если БД пуста")
    p_dl.add_argument("--retry", action="store_true", help="После основного прохода один раз прогнать сбойные (failed → pending)")
    p_dl.add_argument(
        "--selection",
        action="store_true",
        help="Скачать только отмеченное в окне (selection.json), статусы базы не менять",
    )
    p_dl.add_argument(
        "--no-return-lost",
        action="store_true",
        help="Не возвращать сгоревшие файлы в очередь (повторные проходы полного цикла)",
    )
    p_dl.set_defaults(func=cmd_download)

    p_retry = sub.add_parser("retry-failed", help="Перезапустить failed → pending")
    p_retry.add_argument("--workers", type=int, default=None)
    p_retry.set_defaults(func=cmd_retry_failed)

    p_stats = sub.add_parser("stats", help="Показать статистику по БД")
    p_stats.set_defaults(func=cmd_stats)

    p_verify = sub.add_parser("verify", help="Проверить MD5 скачанных файлов")
    p_verify.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    # Если запущен без аргументов — автоматически поднимаем веб-дашборд
    is_no_args = (argv is None and len(sys.argv) == 1) or (argv is not None and len(argv) == 0)
    if is_no_args:
        webui_path = PROJECT_DIR / "webui.py"
        if webui_path.exists():
            import subprocess
            print("=" * 70)
            print("  Запуск единого веб-дашборда Yandex.Disk & Безлимит by Sergey Pukhir (ex3544, vovapuh's son)...")
            print("  Адрес в браузере: http://127.0.0.1:8000")
            print("=" * 70)
            try:
                return subprocess.run([sys.executable, str(webui_path)]).returncode
            except KeyboardInterrupt:
                return 130

    parser = build_parser()
    args = parser.parse_args(argv)

    # stats и verify читают только локальную БД — токен не нужен
    require_token = args.cmd not in ("stats", "verify")
    cfg = load_config(require_token=require_token)
    setup_logging(cfg["log_level"])

    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
