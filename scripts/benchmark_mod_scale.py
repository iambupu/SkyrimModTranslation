"""Synthetic metadata benchmark for large-Mod scale classification."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from audit_mod_scale import (
    AssessmentAccumulator,
    classify_risk,
    classify_scale,
    default_scale_config_path,
    load_scale_config,
    observe_entry,
)
from game_context import load_game_profile
from project_paths import is_under, project_root, relative_path, resolve_project_path
from report_utils import utc_now


def run_benchmark(*, game_id: str, entries: int, bytes_per_file: int) -> dict[str, object]:
    if entries <= 0 or bytes_per_file <= 0:
        raise ValueError("entries and bytes_per_file must be positive")
    context = load_game_profile(game_id)
    config = load_scale_config(default_scale_config_path())
    accumulator = AssessmentAccumulator()
    started = time.perf_counter()
    for index in range(entries):
        if index % 100 == 0:
            path = Path("Scripts") / f"script-{index:08d}.pex"
            size = min(bytes_per_file, 128 * 1024)
        elif index % 10 == 0:
            path = Path("Interface") / "translations" / f"menu-{index:08d}_en.txt"
            size = min(bytes_per_file, 64 * 1024)
        else:
            path = Path("Textures") / f"texture-{index:08d}.dds"
            size = bytes_per_file
        observe_entry(accumulator, context, config, path, size)
    elapsed = time.perf_counter() - started
    scale_level, _ = classify_scale(
        {
            "max_unpacked_bytes": accumulator.estimated_unpacked_bytes,
            "max_file_count": accumulator.file_count,
            "max_candidate_rows": accumulator.estimated_candidate_rows,
            "max_archive_count": accumulator.archive_count,
        },
        config,
    )
    risk_level, _ = classify_risk(accumulator)
    return {
        "schema_version": 1,
        "report_type": "mod-scale-metadata-benchmark",
        "generated_at": utc_now(),
        "game_id": game_id,
        "entries": entries,
        "bytes_per_file": bytes_per_file,
        "elapsed_seconds": elapsed,
        "entries_per_second": entries / elapsed if elapsed else 0,
        "scale_level": scale_level,
        "risk_level": risk_level,
        "candidate_files": accumulator.candidate_file_count,
        "protected_bytes": accumulator.protected_bytes,
        "note": "Synthetic metadata/classification benchmark; it does not claim archive decompression throughput.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark L4/L5 metadata classification without creating Mod files.")
    parser.add_argument("--game", choices=("skyrim-se", "fallout4"), default="skyrim-se")
    parser.add_argument("--entries", type=int, default=100_000)
    parser.add_argument("--bytes-per-file", type=int, default=1024 * 1024)
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()
    report = run_benchmark(game_id=args.game, entries=args.entries, bytes_per_file=args.bytes_per_file)
    if args.report_path:
        root = project_root()
        path = resolve_project_path(root, args.report_path, must_exist=False)
        qa_root = resolve_project_path(root, "qa", must_exist=False)
        if not is_under(path, qa_root):
            raise ValueError("Benchmark report must stay under qa/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Benchmark report: {relative_path(root, path)}")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
