from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from benchmark_mod_scale import run_benchmark  # noqa: E402


def test_synthetic_scale_benchmark_reports_translation_workload_separately() -> None:
    report = run_benchmark(game_id="skyrim-se", entries=1000, bytes_per_file=1024 * 1024)
    assert report["entries"] == 1000
    assert report["entries_per_second"] > 0
    assert report["protected_bytes"] > 0
    assert 0 < report["candidate_files"] < report["entries"]
