from __future__ import annotations

import sys
from pathlib import Path

from .constants import APP_ROOT


def asset_path(name: str) -> Path:
    """Path to bundled UI asset from temp/ for source and PyInstaller builds."""
    rel = Path("temp") / name
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / rel)
    candidates.extend(
        [
            APP_ROOT / rel,
            APP_ROOT.parent / rel,
            Path.cwd() / rel,
            Path.cwd().parent / rel,
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]
