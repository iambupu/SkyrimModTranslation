"""Validate Claude Code marketplace metadata for this repository."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from claude_plugin_marketplace import (
    MARKETPLACE_JSON,
    PLUGIN_JSON,
    config_validation_errors,
    read_json,
    repo_root,
)


def resolve_repo_path(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    root_resolved = root.resolve(strict=True)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"{label} must stay under repository root: {value}") from None
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Claude Code marketplace metadata.")
    parser.add_argument(
        "--marketplace-path",
        default=str(MARKETPLACE_JSON),
        help="Path to .claude-plugin/marketplace.json.",
    )
    parser.add_argument(
        "--plugin-path",
        default=str(PLUGIN_JSON),
        help="Path to .claude-plugin/plugin.json.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON validation report.")
    args = parser.parse_args(argv)

    root = repo_root()
    errors: list[str] = []
    marketplace_path: Path | None = None
    plugin_path: Path | None = None
    try:
        marketplace_path = resolve_repo_path(root, args.marketplace_path, "marketplace path")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        plugin_path = resolve_repo_path(root, args.plugin_path, "plugin path")
    except ValueError as exc:
        errors.append(str(exc))

    marketplace_payload = None
    plugin_payload = None
    if marketplace_path is not None:
        try:
            marketplace_payload = read_json(marketplace_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"marketplace JSON is invalid: {exc}")
    if plugin_path is not None:
        try:
            plugin_payload = read_json(plugin_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"plugin JSON is invalid: {exc}")

    if isinstance(marketplace_payload, dict) and isinstance(plugin_payload, dict):
        errors.extend(config_validation_errors(marketplace_payload, plugin_payload, root=root))

    report = {
        "marketplace_path": str(marketplace_path or args.marketplace_path),
        "plugin_path": str(plugin_path or args.plugin_path),
        "valid": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif errors:
        print("FAIL: Claude Code marketplace metadata is invalid.")
        for error in errors:
            print(f"- {error}")
    else:
        print("PASS: Claude Code marketplace metadata is valid.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
