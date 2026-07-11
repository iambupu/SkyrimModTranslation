"""List portable runtime Skills for supported agents."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from agent_capabilities import GUI_ONLY_RUNTIME_SKILLS, SUPPORTED_AGENTS, load_agent_capabilities
from project_paths import plugin_root, relative_path


FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", re.DOTALL)


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\"")
    return metadata


def skill_rows(agent: str) -> list[dict[str, Any]]:
    root = plugin_root()
    load_agent_capabilities()
    rows: list[dict[str, Any]] = []
    for skill_dir in sorted((root / "skills").iterdir(), key=lambda item: item.name.lower()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        metadata = parse_frontmatter(skill_file)
        gui_only = skill_dir.name in GUI_ONLY_RUNTIME_SKILLS
        usable = not (agent != "codex" and gui_only)
        rows.append(
            {
                "agent": agent,
                "skill_dir": skill_dir.name,
                "name": metadata.get("name", ""),
                "description": metadata.get("description", ""),
                "path": relative_path(root, skill_file),
                "requires_gui": gui_only,
                "usable": usable,
                "reason": "" if usable else "Codex-only GUI automation skill.",
            }
        )
    return rows


def print_table(rows: list[dict[str, Any]]) -> None:
    print("| Skill | Usable | GUI | Description |")
    print("|---|---:|---:|---|")
    for row in rows:
        description = str(row.get("description", "")).replace("|", "\\|")
        print(
            f"| {row.get('skill_dir', '')} | {str(row.get('usable', False)).lower()} | "
            f"{str(row.get('requires_gui', False)).lower()} | {description} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="List runtime Skills visible to an agent adapter.")
    parser.add_argument("--agent", choices=sorted(SUPPORTED_AGENTS), default="codex")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()

    rows = skill_rows(args.agent)
    if args.format == "json":
        print(json.dumps({"agent": args.agent, "skills": rows}, ensure_ascii=False, indent=2))
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
