"""Normalize exported translation rows into project JSONL shape."""

import argparse
import shutil
import sys
from pathlib import Path
from file_utils import read_valid_jsonl_lines
from project_paths import project_root
from project_paths import is_under, resolve_project_path






def validate_jsonl(path: Path) -> int:
    return len(read_valid_jsonl_lines(path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize a project-local JSONL export into work/normalized without modifying content.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-dir", default="work/normalized")
    args = parser.parse_args()

    root = project_root()
    input_path = resolve_project_path(root, args.input_path, must_exist=True)
    output_dir = resolve_project_path(root, args.output_dir, must_exist=False)
    if not is_under(output_dir, root / "work"):
        raise ValueError("OutputDir must be under work/.")
    output_dir.mkdir(parents=True, exist_ok=True)
    line_count = validate_jsonl(input_path)

    output_path = output_dir / f"{input_path.stem}.normalized.jsonl"
    if output_path.resolve(strict=False) == input_path.resolve(strict=True):
        raise ValueError("Output path would overwrite the original file.")
    shutil.copyfile(input_path, output_path)

    print("JSONL input copied without modification.")
    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    print(f"Lines: {line_count}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Normalize export failed: {exc}", file=sys.stderr)
        sys.exit(1)
