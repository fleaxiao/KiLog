from __future__ import annotations

from pathlib import Path
import sys


PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
sys.path.insert(0, str(PLUGIN))
