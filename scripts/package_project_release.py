"""Package the project release from Git-tracked files only.

This creates a source/project release archive, not a Skyrim final_mod package.
The file list comes from Git so ignored local outputs and untracked files never
enter the zip.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from project_paths import relative_path, resolve_project_path, safe_file_name
from file_utils import sha256_file, validate_regular_path_under
from project_paths import source_repo_root as source_root


DEFAULT_VERSION = "1.0.1"
DEFAULT_PACKAGE_NAME = "SkyrimModTranslation"
DEFAULT_OUTPUT_DIR = Path("out") / "project_packages"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")


@dataclass
class PackageManifest:
    PackageName: str
    Version: str
    ArchivePath: str
    ArchiveSha256: str
    FileCount: int
    TotalInputBytes: int
    GeneratedAtUtc: str
    GitCommit: str
    GitDirty: bool
    GitTrackedFilesOnly: bool
    UntrackedFilesExcluded: int
    UntrackedFileSamples: list[str]
    ExclusionPolicy: str
    Files: list[dict[str, object]]


def run_git(root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return completed


def git_tracked_files(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    completed = run_git(root, ["ls-files", "-z"])
    files: list[Path] = []
    for raw in completed.stdout.split("\0"):
        if not raw:
            continue
        candidate = root / raw
        if not candidate.exists():
            # A tracked deletion is part of the current working-tree release view.
            continue
        path = validate_regular_path_under(
            candidate,
            root,
            kind="file",
            label=f"tracked release file {raw}",
        )
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().lower())


def git_commit(root: Path) -> str:
    completed = run_git(root, ["rev-parse", "--verify", "HEAD"], check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def git_dirty(root: Path) -> bool:
    completed = run_git(root, ["status", "--porcelain"], check=False)
    if completed.returncode != 0:
        return True
    return bool(completed.stdout.strip())


def git_untracked_files(root: Path) -> list[str]:
    completed = run_git(root, ["ls-files", "--others", "--exclude-standard", "-z"], check=False)
    if completed.returncode != 0:
        return []
    return sorted((raw for raw in completed.stdout.split("\0") if raw), key=str.lower)



def validate_version(value: str) -> str:
    version = value.strip()
    if not VERSION_PATTERN.match(version):
        raise argparse.ArgumentTypeError(
            "version may contain only letters, numbers, dot, underscore, and hyphen"
        )
    return version




def archive_entry_name(package_name: str, version: str, root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return f"{package_name}-{version}/{relative}"


def write_zip(root: Path, files: list[Path], package_name: str, version: str, archive_path: Path) -> tuple[int, list[dict[str, object]]]:
    total_bytes = 0
    rows: list[dict[str, object]] = []
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            entry_name = archive_entry_name(package_name, version, root, path)
            size = path.stat().st_size
            digest = sha256_file(path)
            archive.write(path, entry_name)
            total_bytes += size
            rows.append(
                {
                    "Path": relative,
                    "ArchiveEntry": entry_name,
                    "SizeBytes": size,
                    "Sha256": digest,
                }
            )
    return total_bytes, rows


def build_manifest(
    root: Path,
    package_name: str,
    version: str,
    archive_path: Path,
    archive_sha256: str,
    total_bytes: int,
    rows: list[dict[str, object]],
    untracked_files: list[str],
) -> PackageManifest:
    return PackageManifest(
        PackageName=package_name,
        Version=version,
        ArchivePath=relative_path(root, archive_path),
        ArchiveSha256=archive_sha256,
        FileCount=len(rows),
        TotalInputBytes=total_bytes,
        GeneratedAtUtc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        GitCommit=git_commit(root),
        GitDirty=git_dirty(root),
        GitTrackedFilesOnly=True,
        UntrackedFilesExcluded=len(untracked_files),
        UntrackedFileSamples=untracked_files[:20],
        ExclusionPolicy="Only files returned by 'git ls-files' are packaged; ignored and untracked files are excluded.",
        Files=rows,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a project release zip from Git-tracked files only."
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        type=validate_version,
        help=f"release version to use in the package name, default: {DEFAULT_VERSION}",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_PACKAGE_NAME,
        help=f"package name prefix, default: {DEFAULT_PACKAGE_NAME}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"project-local output directory, default: {DEFAULT_OUTPUT_DIR.as_posix()}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned package path and file count without writing the zip",
    )
    parser.add_argument(
        "--allow-untracked-excluded",
        action="store_true",
        help=(
            "allow packaging even when non-ignored untracked files exist; "
            "by default source releases fail fast so new production files are not accidentally omitted"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = source_root()
    package_name = safe_file_name(str(args.name).strip() or DEFAULT_PACKAGE_NAME)
    output_dir = resolve_project_path(root, args.output_dir, must_exist=False)
    archive_path = output_dir / f"{package_name}-{args.version}.zip"
    manifest_path = output_dir / f"{package_name}-{args.version}.manifest.json"

    files = git_tracked_files(root)
    if not files:
        raise RuntimeError("no Git-tracked files found; refusing to create an empty package")
    untracked_files = git_untracked_files(root)

    if args.dry_run:
        print(f"Package: {relative_path(root, archive_path)}")
        print(f"Version: {args.version}")
        print(f"Tracked files: {len(files)}")
        print(f"Untracked files excluded: {len(untracked_files)}")
        for path in untracked_files[:20]:
            print(f"  - {path}")
        if untracked_files and not args.allow_untracked_excluded:
            print("Non-dry-run packaging will fail unless these files are tracked, ignored, or --allow-untracked-excluded is used.")
        print("Mode: dry-run")
        return 0

    if untracked_files and not args.allow_untracked_excluded:
        print("Error: non-ignored untracked files would be excluded from the source package.")
        for path in untracked_files[:20]:
            print(f"  - {path}")
        print("Track production files before packaging, ignore local-only files, or rerun with --allow-untracked-excluded.")
        return 1

    total_bytes, rows = write_zip(root, files, package_name, args.version, archive_path)
    archive_sha256 = sha256_file(archive_path)
    manifest = build_manifest(root, package_name, args.version, archive_path, archive_sha256, total_bytes, rows, untracked_files)
    manifest_path.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"Package: {relative_path(root, archive_path)}")
    print(f"Manifest: {relative_path(root, manifest_path)}")
    print(f"Version: {args.version}")
    print(f"Tracked files: {len(rows)}")
    print(f"Archive SHA256: {archive_sha256}")
    if untracked_files:
        print(f"Warning: {len(untracked_files)} untracked files were excluded from the package.")
    if manifest.GitDirty:
        print("Warning: working tree has uncommitted changes; packaged tracked files reflect the current working tree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
