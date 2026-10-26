"""Генерирует assets/app.png, assets/tray.png и assets/app.ico.

Запуск из корня проекта:
    python tools/make_icons.py

Иконки — простые геометрические фигуры без внешних ресурсов, чтобы их можно
было пересобрать на любой машине.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


def draw_icon(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Вертикальный градиент синего.
    for y in range(size):
        t = y / max(1, size - 1)
        color = (
            int(37 + (27 - 37) * t),
            int(104 + (74 - 104) * t),
            int(240 + (210 - 240) * t),
            255,
        )
        draw.line([(0, y), (size, y)], fill=color)

    # Скруглённый квадрат.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255
    )
    img.putalpha(mask)

    white = (255, 255, 255, 255)
    # Туча: три круга + скруглённая база.
    for cx, cy, r in (
        (0.355, 0.455, 0.150),
        (0.500, 0.385, 0.205),
        (0.645, 0.455, 0.150),
    ):
        draw.ellipse(
            [
                size * (cx - r),
                size * (cy - r),
                size * (cx + r),
                size * (cy + r),
            ],
            fill=white,
        )
    draw.rounded_rectangle(
        [size * 0.205, size * 0.455, size * 0.795, size * 0.600],
        radius=int(size * 0.072),
        fill=white,
    )

    # Жёлтая стрелка скачивания.
    yellow = (255, 204, 0, 255)
    draw.rounded_rectangle(
        [size * 0.445, size * 0.585, size * 0.555, size * 0.765],
        radius=int(size * 0.018),
        fill=yellow,
    )
    draw.polygon(
        [
            (size * 0.345, size * 0.720),
            (size * 0.655, size * 0.720),
            (size * 0.500, size * 0.885),
        ],
        fill=yellow,
    )
    return img


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = draw_icon(1024)
    icon.resize((256, 256), Image.LANCZOS).save(ASSETS / "app.png")
    icon.resize((64, 64), Image.LANCZOS).save(ASSETS / "tray.png")
    icon.save(
        ASSETS / "app.ico",
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print(f"Готово: {ASSETS / 'app.ico'}, app.png, tray.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
