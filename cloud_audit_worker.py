"""Воркер сверки с облаком (35.7.6).

Запуск:
    python cloud_audit_worker.py --scope both
    python cloud_audit_worker.py --scope disk
    python cloud_audit_worker.py --scope unlim

Идёт по облаку онлайн, сравнивает с базой и пишет прогресс в
`logs/cloud_audit_status.json`, отчёт `logs/cloud_audit_report.txt` и полный
список `logs/cloud_audit.csv`. Ничего не скачивает, не удаляет и статусы в
базе не меняет.
"""

from __future__ import annotations

import argparse
import sys

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import cloud_audit  # noqa: E402
import paths  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloud_audit_worker",
        description="Сверить, что сейчас лежит в облаке, с базой программы.",
    )
    p.add_argument(
        "--scope",
        choices=["disk", "unlim", "both"],
        default="both",
        help="какие базы сверять (по умолчанию обе)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        f"[i] Сверка с облаком: {cloud_audit.SCOPE_NAMES.get(args.scope, args.scope)}",
        flush=True,
    )

    last = {"stage": "", "count": 0}

    def progress(stage: str, count: int) -> None:
        if stage == last["stage"] and count - last["count"] < 500:
            return
        last["stage"], last["count"] = stage, count
        print(
            f"[К] {cloud_audit.SCOPE_NAMES.get(stage, stage)}: прочитано {count:,} файлов",
            flush=True,
        )

    try:
        data = cloud_audit.run(args.scope, on_progress=progress,
                               on_error=lambda text: print(f"[!] {text}", flush=True))
    except cloud_audit.AuditError as exc:
        cloud_audit.write_error(args.scope, str(exc))
        print(f"[ОШИБКА] {exc}", flush=True)
        return 2
    except KeyboardInterrupt:
        cloud_audit.write_error(args.scope, "сверка остановлена")
        print("[!] Сверка остановлена.", flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001 — понятная причина вместо трейсбека
        cloud_audit.write_error(args.scope, str(exc))
        print(f"[ОШИБКА] {exc}", flush=True)
        return 1

    print(
        f"\n[Итог] ✅ совпадает {data.get('same', 0):,} · "
        f"➕ новое {data.get('new', 0):,} · "
        f"🔁 изменилось {data.get('changed', 0):,} · "
        f"➖ пропало {data.get('gone', 0):,}",
        flush=True,
    )
    if data.get("errors"):
        print(f"[!] Не удалось прочитать: {len(data['errors'])} — подробности в отчёте.",
              flush=True)
    print(f"Отчёт: {paths.CLOUD_AUDIT_REPORT_FILE}", flush=True)
    print(f"Полный список: {paths.CLOUD_AUDIT_CSV_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ОШИБКА] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
