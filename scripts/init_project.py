"""Compatibility wrapper for scripts/init_workspace.py."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("init_workspace.py")), run_name="__main__")
