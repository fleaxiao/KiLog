from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
PACKAGE = ROOT / "package"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VERSION = "1.0.1"


def remove_readonly(func, path: str, _error) -> None:
    """Let clean builds replace files marked read-only by Windows or OneDrive."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def zip_tree(source: Path, destination: Path, prefix: str = "") -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(source).as_posix()
                archive.write(path, f"{prefix}{relative}")


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD, onerror=remove_readonly)
    BUILD.mkdir(parents=True)
    DIST.mkdir(parents=True, exist_ok=True)

    pcm_root = BUILD / "pcm"
    shutil.copytree(PLUGIN, pcm_root / "plugins")
    shutil.copy2(PACKAGE / "metadata.json", pcm_root / "metadata.json")
    resources = pcm_root / "resources"
    resources.mkdir()
    shutil.copy2(PLUGIN / "assets" / "icon-ui-64.png", resources / "icon.png")

    manual_zip = DIST / f"kilog-plugin-{VERSION}.zip"
    pcm_zip = DIST / f"kilog-pcm-{VERSION}.zip"
    zip_tree(PLUGIN, manual_zip, prefix="kilog/")
    zip_tree(pcm_root, pcm_zip)
    print(manual_zip)
    print(pcm_zip)


if __name__ == "__main__":
    main()
