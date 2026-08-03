"""Build all KiLog PNG sizes from the generated master artwork."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "plugin" / "assets"
SOURCE = ROOT / "design" / "generated" / "kilog-logo-imagegen.png"
OUTPUTS = {
    "icon-ui-64.png": 64,
    "icon-256.png": 256,
    "icon-toolbar-light-24.png": 24,
    "icon-toolbar-light-48.png": 48,
    "icon-toolbar-dark-24.png": 24,
    "icon-toolbar-dark-48.png": 48,
}


def load_master() -> Image.Image:
    image = Image.open(SOURCE).convert("RGBA")
    if image.width != image.height:
        raise ValueError(f"Logo source must be square, got {image.size}")

    # Image generation supplies a dark canvas around the rounded tile. Replace it
    # with alpha so the icon sits cleanly on KiCad's light and dark toolbars.
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1),
        radius=round(image.width * 0.15),
        fill=255,
    )
    image.putalpha(mask)
    return image


def main() -> None:
    master = load_master()
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, size in OUTPUTS.items():
        path = ASSETS / name
        master.resize((size, size), Image.Resampling.LANCZOS).save(path, optimize=True)
        print(f"rendered {path.relative_to(ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
