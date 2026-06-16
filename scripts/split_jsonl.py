import argparse
import json
import os
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


def read_valid_jsonl(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"line {index} is empty; JSONL requires one JSON object per line")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {index} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"line {index} is not a JSON object")
    return lines


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
