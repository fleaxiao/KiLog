"""Build the window, README and toolbar icons from assets/icon.svg."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from resvg_py import svg_to_bytes


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "icon.svg"
OUTPUTS = {
    "icon-ui-64.png": 64,
    "icon-256.png": 256,
    "icon-toolbar-light-24.png": 24,
    "icon-toolbar-light-48.png": 48,
    "icon-toolbar-dark-24.png": 24,
    "icon-toolbar-dark-48.png": 48,
}


def load_master() -> Image.Image:
    # Render the vector at 4x the largest output, then downsample for smooth
    # fine strokes and transparent rounded corners at toolbar sizes.
    size = max(OUTPUTS.values()) * 4
    png = svg_to_bytes(
        svg_path=str(SOURCE), width=size, height=size, skip_system_fonts=True
    )
    with Image.open(BytesIO(png)) as image:
        return image.convert("RGBA")


def main() -> None:
    master = load_master()
    ASSETS.mkdir(parents=True, exist_ok=True)
    # The green tile keeps the same orange artwork legible on both themes.
    for name, size in OUTPUTS.items():
        path = ASSETS / name
        master.resize((size, size), Image.Resampling.LANCZOS).save(path, optimize=True)
        print(f"rendered {path.relative_to(ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
