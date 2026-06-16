"""Clean a generated final_mod directory within the required output layout."""

import argparse
import shutil
from pathlib import Path

from project_paths import LOCALIZATION_OUTPUT_DIR
from route_translation_task import is_under, project_root, relative_path, resolve_project_path


PROTECTED_PROJECT_DIRS = {"mod", "source", "work", "translated", "qa"}


def is_final_mod_dir(root: Path, final_mod_dir: Path) -> bool:
    out_root = resolve_project_path(root, "out", must_exist=False)
    if not is_under(final_mod_dir, out_root):
        return False
    try:
        relative = final_mod_dir.resolve(strict=False).relative_to(out_root.resolve(strict=False))
    except ValueError:
        return False
    return len(relative.parts) == 3 and relative.parts[1] == LOCALIZATION_OUTPUT_DIR and relative.parts[2].lower() == "final_mod"


def confirm_delete(path: Path) -> bool:
    answer = input(f"Clean final mod directory '{path}'? Type YES to continue: ")
    return answer == "YES"


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean a project-local out/<ModName>/汉化产出/final_mod directory.")
    parser.add_argument("--final-mod-dir", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    final_mod_dir = resolve_project_path(root, args.final_mod_dir, must_exist=True)
    if not final_mod_dir.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {args.final_mod_dir}")
    if not is_final_mod_dir(root, final_mod_dir):
        raise ValueError(f"Refusing to clean anything except out/<ModName>/汉化产出/final_mod: {args.final_mod_dir}")

    for name in PROTECTED_PROJECT_DIRS:
        protected = resolve_project_path(root, name, must_exist=True)
        if final_mod_dir.resolve(strict=True) == protected.resolve(strict=True):
            raise ValueError(f"Refusing to delete protected project directory: {name}")

    if args.dry_run:
        print(f"Dry run: would clean final mod directory: {final_mod_dir}")
        return 0

    if not args.force and not confirm_delete(final_mod_dir):
        print("Clean cancelled.")
        return 0

    shutil.rmtree(final_mod_dir)
    print(f"Cleaned final mod directory: {final_mod_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
