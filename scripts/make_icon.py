#!/usr/bin/env python3
"""Draw the app icon, so it can be redrawn rather than rediscovered.

The build shipped with ``icon=None`` — a blank Python rocket in the Dock, which does not
say "this is the tool that tidies my papers". Checked in as code rather than as a binary
so a colour or a corner radius is a one-line change, and so the .icns can be rebuilt on
any machine.

    python3 scripts/make_icon.py     # writes assets/icon.icns

The design: a macOS-style rounded square in a blue→indigo gradient, a white exam paper
sitting slightly proud of centre with a few lines of text on it, and a green check badge
in the corner. It has to survive being shrunk to 16px in the menu bar, so the paper is
large, the contrast is high, and there is no fine detail to lose.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "icon.icns"

SIZE = 1024
BG_TOP = (59, 106, 224)      # blue
BG_BOTTOM = (99, 74, 208)    # indigo
PAPER = (255, 255, 255)
INK = (150, 166, 196)
TITLE_INK = (72, 96, 152)
CHECK_BG = (46, 184, 114)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _gradient(size: int) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    pixels = grad.load()
    for y in range(size):
        t = y / (size - 1)
        pixels[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
    return grad.resize((size, size))


def draw(size: int = SIZE) -> Image.Image:
    s = size / 1024  # everything below is authored at 1024 and scaled

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # macOS "squircle": the radius is a little under a quarter of the side.
    icon.paste(_gradient(size), (0, 0), _rounded_mask(size, round(228 * s)))

    draw = ImageDraw.Draw(icon)

    # The paper. Offset up and left so the check badge has somewhere to sit.
    px0, py0, px1, py1 = 236 * s, 196 * s, 788 * s, 828 * s
    draw.rounded_rectangle(
        (px0, py0, px1, py1), radius=round(28 * s),
        fill=PAPER + (255,), outline=(30, 50, 110, 40), width=max(1, round(3 * s)),
    )

    # A title bar and lines of text: legible as "a paper" even at 16px, where the
    # individual lines merge into a grey block and that is exactly right.
    left, right = px0 + 66 * s, px1 - 66 * s
    y = py0 + 96 * s
    draw.rounded_rectangle(
        (left, y, left + (right - left) * 0.55, y + 34 * s),
        radius=round(17 * s), fill=TITLE_INK + (255,),
    )

    y += 104 * s
    for width in (1.0, 0.92, 1.0, 0.78, 1.0, 0.86):
        draw.rounded_rectangle(
            (left, y, left + (right - left) * width, y + 24 * s),
            radius=round(12 * s), fill=INK + (255,),
        )
        y += 66 * s

    # The check: "sorted". Drawn as a badge so it reads against the paper and the
    # background alike.
    cx, cy, r = 760 * s, 772 * s, 168 * s
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))
    draw.ellipse(
        (cx - r + 16 * s, cy - r + 16 * s, cx + r - 16 * s, cy + r - 16 * s),
        fill=CHECK_BG + (255,),
    )
    draw.line(
        [(cx - 74 * s, cy + 4 * s), (cx - 18 * s, cy + 60 * s), (cx + 78 * s, cy - 58 * s)],
        fill=(255, 255, 255, 255), width=round(40 * s), joint="curve",
    )
    return icon


def build() -> Path:
    if not shutil.which("iconutil"):
        raise SystemExit("iconutil not found — this only builds on macOS.")

    master = draw(SIZE)
    iconset = ROOT / "assets" / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    # The set Apple asks for: each size, and its @2x retina twin.
    for base in (16, 32, 128, 256, 512):
        master.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
        master.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    shutil.rmtree(iconset)

    # Kept alongside the .icns: Qt cannot load .icns for setWindowIcon, and it is what
    # the README and any future web page would use.
    master.save(ROOT / "assets" / "icon.png")
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    sys.exit(0)
