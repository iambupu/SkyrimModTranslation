"""Export a compact context packet for an agent adapter."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from agent_capabilities import ALLOWED_HANDOFF_FILES as DEFAULT_AGENT_HANDOFF_FILES
from agent_capabilities import SUPPORTED_AGENTS, agent_config, load_agent_capabilities
from list_agent_skills import skill_rows
from project_paths import is_under, project_root, relative_path, resolve_project_path
from write_agent_handoff import evaluate_resume_checkpoint


KNOWN_HANDOFF_FILES = {"qa/agent_handoff.json", "qa/codex_handoff.json"}
GAME_CONTEXT_FIELDS = (
    "game_id",
    "game_profile_version",
    "game_display_name",
    "support_level",
    "plugin_adapter",
    "plugin_adapter_version",
    "pex_category",
    "pex_writeback_status",
    "archive_delivery",
    "archive_allow_repack",
)


def normalized_project_path(value: Path) -> str:
    return value.as_posix().strip("/")


def resolve_agent_handoff_path(root: Path, raw_path: object) -> Path:
    value = str(raw_path or "qa/agent_handoff.json").strip() or "qa/agent_handoff.json"
    path = resolve_project_path(root, value, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(path, qa_root):
        raise ValueError(f"agent handoff file must stay under qa/: {value}")
    if path.suffix.lower() != ".json":
        raise ValueError(f"agent handoff file must be a JSON file: {value}")
    try:
        normalized = normalized_project_path(path.relative_to(root.resolve(strict=False)))
    except ValueError:
        normalized = normalized_project_path(Path(value))
    if normalized not in KNOWN_HANDOFF_FILES:
        raise ValueError(f"agent handoff file must be qa/agent_handoff.json or qa/codex_handoff.json: {value}")
    return path


def adapter_handoff_file(agent: str, adapter: dict[str, object]) -> str:
    value = str(adapter.get("handoff_file", "")).strip()
    if value:
        return value
    return DEFAULT_AGENT_HANDOFF_FILES[agent]


def resolve_context_output_path(root: Path, raw_path: str, agent: str) -> Path:
    if raw_path:
        path = resolve_project_path(root, raw_path, must_exist=False)
    else:
        path = (
            root
            / "qa"
            / "agent_context_prompts"
            / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.{agent}.context.md"
        )
    output_root = resolve_project_path(root, "qa/agent_context_prompts", must_exist=False)
    if not is_under(path, output_root):
        raise ValueError(f"agent context output must stay under qa/agent_context_prompts/: {raw_path or path}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"agent context output must be a Markdown file under qa/agent_context_prompts/: {raw_path or path}")
    return path


def read_json_block_if_exists(path: Path, *, max_chars: int = 12000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"format": "text", "content": text}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(rendered) <= max_chars:
        return rendered
    summary = {
        "truncated": True,
        "source_path": str(path),
        "original_chars": len(rendered),
        "excerpt": rendered[:max_chars],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def read_game_context_summary(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {field: payload[field] for field in GAME_CONTEXT_FIELDS if field in payload}


def handoff_checkpoint_freshness(root: Path, path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"fresh": False, "status": "missing", "reasons": [{"reason": "handoff_missing"}]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"fresh": False, "status": "invalid", "reasons": [{"reason": str(exc)}]}
    checkpoint = payload.get("resume_checkpoint", {}) if isinstance(payload, dict) else {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    return evaluate_resume_checkpoint(root, checkpoint)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a bounded context packet for an agent adapter.")
    parser.add_argument("--agent", choices=sorted(SUPPORTED_AGENTS), required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    root = project_root()
    capabilities = load_agent_capabilities()
    adapter = agent_config(capabilities, args.agent)
    try:
        output = resolve_context_output_path(root, args.output, args.agent)
        handoff_path = resolve_agent_handoff_path(root, adapter_handoff_file(args.agent, adapter))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    if not output.parent.is_dir():
        output.parent.mkdir(parents=True, exist_ok=True)

    codex_handoff_path = root / "qa" / "codex_handoff.json"
    handoff_heading = "Codex Handoff" if handoff_path == codex_handoff_path else "Agent Handoff"
    agent_handoff_text = read_json_block_if_exists(handoff_path)
    game_context = read_game_context_summary(handoff_path)
    fallback_handoff_text = "" if handoff_path == codex_handoff_path else read_json_block_if_exists(codex_handoff_path)
    freshness = (
        handoff_checkpoint_freshness(root, handoff_path)
        if handoff_path != codex_handoff_path
        else {"fresh": True, "status": "not_applicable", "reasons": []}
    )
    skills = skill_rows(args.agent)
    visible_skills = [row for row in skills if row.get("usable")]
    gui_blocked = [row for row in skills if row.get("requires_gui") and not row.get("usable")]

    packet = [
        f"# Agent Context: {args.agent}",
        "",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {root}",
        f"- Task id: {args.task_id}",
        f"- Support level: {adapter.get('support_level', '')}",
        f"- GUI automation: {adapter.get('supports_gui_automation', False)}",
        f"- Computer Use: {adapter.get('supports_computer_use', False)}",
        "- Game authority: workspace marker and exported Game Profile; never infer from a Mod name.",
        "",
        "## Game Profile",
        "",
        "```json",
        json.dumps(game_context, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Hard Rules",
        "",
        "- Use only project Python entrypoints.",
        "- Do not edit `qa/workflow_tasks.json` directly.",
        "- Do not access real game, MO2, Vortex, Steam, AppData, or `Documents/My Games` paths.",
        "- Do not directly edit binary plugin, archive, PEX, DLL, or executable files.",
        "- GUI, Computer Use, pywinauto, UI Automation, and desktop coordinates are Codex-only.",
        "- Non-GUI adapters must return `blocked` with `handoff_target=codex` for GUI-only tasks.",
        "- Check `Resume Checkpoint Freshness` before trusting this packet; refresh the state chain when it is stale.",
        "",
        "## Resume Checkpoint Freshness",
        "",
        "```json",
        json.dumps(freshness, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Adapter Manifest",
        "",
        "```json",
        json.dumps(adapter, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Usable Skills",
        "",
    ]
    for row in visible_skills:
        packet.append(f"- `{row.get('skill_dir', '')}`: {row.get('description', '')}")
    if gui_blocked:
        packet.extend(["", "## Codex-Only GUI Skills", ""])
        for row in gui_blocked:
            packet.append(f"- `{row.get('skill_dir', '')}`")
    packet.extend(["", f"## {handoff_heading}", "", "```json", agent_handoff_text or "{}", "```"])
    if fallback_handoff_text:
        packet.extend(["", "## Codex Handoff Fallback", "", "```json", fallback_handoff_text, "```"])
    output.write_text("\n".join(packet) + "\n", encoding="utf-8")
    print(f"Agent context written to: {relative_path(root, output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
