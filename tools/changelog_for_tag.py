"""Достаёт из CHANGELOG.md раздел для указанной версии.

Используется в GitHub Actions перед публикацией релиза:

    python tools/changelog_for_tag.py v35.4.9 release_notes.md

Если раздела нет, пишет запасной текст, чтобы релиз всё равно опубликовался.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
CHANGELOG = PROJECT_DIR / "CHANGELOG.md"
CHANGELOG_URL = "https://github.com/ex3544/yadisk_backup/blob/main/CHANGELOG.md"


def extract(version: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^## \[(?P<version>[^\]]+)\]", text))
    for index, match in enumerate(matches):
        if match.group("version").strip() != version:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        return f"{section}\n\nПолная история изменений: {CHANGELOG_URL}\n"
    return (
        f"## Yandex.Disk Backup {version}\n\n"
        f"Список изменений для этой версии не найден.\n\n"
        f"Полная история изменений: {CHANGELOG_URL}\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: changelog_for_tag.py <tag> <output-file>", file=sys.stderr)
        return 2
    tag, output = argv[1], Path(argv[2])
    version = tag.strip().removeprefix("v")
    output.write_text(extract(version), encoding="utf-8")
    print(f"release notes: {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
