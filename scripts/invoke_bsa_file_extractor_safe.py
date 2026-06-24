"""Safely invoke the project-local BSAFileExtractor tool.

This wrapper only accepts project-local BSA input and project-local output
under work/archive_extracts/. It never modifies the source archive.
"""

import argparse
import subprocess
import sys

from project_paths import is_under, project_root, relative_path, resolve_project_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-local safe wrapper for BSAFileExtractor.")
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tool-path", default="tools/BSAFileExtractor/BSAFileExtractor.py")
    parser.add_argument("--filter", action="append", default=[], help="Optional file path substring to extract. Repeat for multiple filters.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show-header", action="store_true")
    args = parser.parse_args()

    root = project_root()
    archive_path = resolve_project_path(root, args.archive_path, must_exist=True)
    output_dir = resolve_project_path(root, args.output_dir, must_exist=False)
    tool_path = resolve_project_path(root, args.tool_path, must_exist=True)
    archive_extracts_root = resolve_project_path(root, "work/archive_extracts", must_exist=False)

    if archive_path.suffix.lower() != ".bsa":
        raise ValueError(f"BSAFileExtractor only supports .bsa input: {relative_path(root, archive_path)}")
    if not is_under(output_dir, archive_extracts_root):
        raise ValueError("BSA extraction output must be under work/archive_extracts/.")

    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(tool_path),
        archive_path.name,
        "-i",
        str(archive_path.parent),
        "-o",
        str(output_dir),
    ]
    if args.show_header:
        command.append("-h")
    if args.verbose:
        command.append("-v")
    command.extend(args.filter)

    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
