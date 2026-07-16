"""Split normalized JSONL into deterministic batch files."""

import argparse
import sys
from pathlib import Path
from file_utils import read_valid_jsonl_lines as read_valid_jsonl
from project_paths import project_root
from project_paths import is_under, resolve_project_path






def main() -> int:
    parser = argparse.ArgumentParser(description="Split a project-local JSONL file into deterministic work/batches JSONL chunks.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-dir", default="work/batches")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    if args.batch_size < 1:
        raise ValueError("BatchSize must be greater than 0.")

    root = project_root()
    input_path = resolve_project_path(root, args.input_path, must_exist=True)
    output_dir = resolve_project_path(root, args.output_dir, must_exist=False)
    if not is_under(output_dir, root / "work"):
        raise ValueError("OutputDir must be under work/.")
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = read_valid_jsonl(input_path)

    written: list[Path] = []
    for index, offset in enumerate(range(0, len(lines), args.batch_size), start=1):
        output_path = output_dir / f"batch_{index:03d}.jsonl"
        batch_lines = lines[offset : offset + args.batch_size]
        output_path.write_text("\n".join(batch_lines) + ("\n" if batch_lines else ""), encoding="utf-8")
        written.append(output_path)

    print(f"Input: {input_path}")
    print(f"OutputDir: {output_dir}")
    print(f"Lines: {len(lines)}")
    print(f"BatchSize: {args.batch_size}")
    print(f"Batches written: {len(written)}")
    for path in written:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Split JSONL failed: {exc}", file=sys.stderr)
        sys.exit(1)
