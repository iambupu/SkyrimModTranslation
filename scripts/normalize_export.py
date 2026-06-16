"""Normalize exported translation rows into project JSONL shape."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def validate_jsonl(path: Path) -> int:
    raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    for index, line in enumerate(raw_lines, start=1):
        if not line.strip():
            raise ValueError(f"line {index} is empty; JSONL requires one JSON object per line")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {index} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"line {index} is not a JSON object")
    return len(raw_lines)


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
