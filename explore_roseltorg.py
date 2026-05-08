"""Разведочный запуск для Росэлторга.

Переиспользует существующий механизм снятия слепков из explore_etp.py:
скриншоты, HTML, cookies, DOM, storage, network log и итоговый report.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

import explore_etp


SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "exploration_results_roseltorg"


def main(argv: list[str] | None = None) -> int:
    explore_etp.TARGET_URL = "https://business.roseltorg.ru/"
    explore_etp.RESULTS_DIR = RESULTS_DIR
    return explore_etp.main(argv)


if __name__ == "__main__":
    sys.exit(main())
