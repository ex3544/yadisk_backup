"""Проверка скачанных файлов из окна программы (воркер C1).

Запуск:
    python verify_local.py --scope disk
    python verify_local.py --scope both --deep
    python verify_local.py --scope unlim --limit 100

Пишет прогресс в `logs/verify_status.json` (его читает панель) и отчёт
`logs/verify_report.md`. Ничего не удаляет и статусы в базе не меняет.
"""

from __future__ import annotations

import argparse
import sys

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import paths  # noqa: E402
import verify  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify_local",
        description="Проверить, что скачанные файлы на месте и совпадают с базой.",
    )
    p.add_argument(
        "--scope",
        choices=["disk", "unlim", "both"],
        default="disk",
        help="какую базу проверять (по умолчанию Диск)",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help="сверять ещё и MD5 — читает весь объём с диска, идёт долго",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="проверить только первые N файлов (0 — все)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = args.limit if args.limit and args.limit > 0 else None
    print(
        f"[i] Проверка: {args.scope}, "
        f"{'размер и MD5' if args.deep else 'только размер'}"
        + (f", до {limit} файлов" if limit else ""),
        flush=True,
    )

    def progress(counts: verify.VerifyCounts) -> None:
        if counts.checked % 500 == 0:
            print(
                f"[К] {counts.checked}/{counts.total or '?'} · "
                f"проблем: {counts.missing + counts.size + counts.md5 + counts.errors}",
                flush=True,
            )

    try:
        data = verify.run(args.scope, deep=args.deep, limit=limit, on_progress=progress)
    except KeyboardInterrupt:
        print("[!] Проверка остановлена.", flush=True)
        return 130

    report = verify.render_report(data)
    try:
        paths.VERIFY_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.VERIFY_REPORT_FILE.write_text(report, encoding="utf-8")
    except OSError as e:
        print(f"[!] Не удалось записать отчёт: {e}", flush=True)

    problems = (data.get("missing", 0) + data.get("size", 0)
                + data.get("md5", 0) + data.get("errors", 0))
    print(
        f"\n[Итог] проверено {data.get('checked', 0)} из {data.get('total', 0)} · "
        f"в порядке {data.get('ok', 0)} · проблем {problems}",
        flush=True,
    )
    if problems:
        print(f"Подробности: {paths.VERIFY_REPORT_FILE}", flush=True)
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — понятная причина вместо трейсбека
        print(f"[ОШИБКА] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
