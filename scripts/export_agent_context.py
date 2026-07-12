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
MAX_PACKET_BYTES = 32768
MAX_NEXT_ACTIONS = 8
MAX_BLOCKERS = 8
MAX_SKILLS = 32
MAX_REFERENCES = 8
MAX_TEXT_CHARS = 512
GAME_CONTEXT_FIELDS = (
    "game_id",
    "game_profile_version",
    "game_display_name",
    "support_level",
    "plugin_adapter",
    "plugin_adapter_version",
    "pex_category",
    "pex_writeback_status",
    "interface_translation_encoding",
    "archive_delivery",
    "archive_allow_repack",
)
ADAPTER_FIELDS = (
    "support_level",
    "levels",
    "supports_controller_mode",
    "supports_codex_plugin",
    "supports_opencode_local_plugins",
    "supports_claude_plugin_marketplace",
    "supports_gui_automation",
    "supports_computer_use",
    "gui_handoff_target",
)
TASK_SUMMARY_FIELDS = (
    "generated_at",
    "total",
    "pending_executable",
    "pending_manual",
    "pending_total",
    "parallel_safe",
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


def read_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_block_if_exists(path: Path, *, max_chars: int = 12000) -> str:
    """Compatibility renderer for callers outside the strict packet builder."""
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
    return json.dumps(
        {
            "truncated": True,
            "source_path": str(path),
            "original_chars": len(rendered),
            "excerpt": rendered[:max_chars],
        },
        ensure_ascii=False,
        indent=2,
    )


def bounded_text(value: object, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    return str(value or "").strip()[:max_chars]


def bounded_strings(value: object, *, limit: int = MAX_REFERENCES) -> list[str]:
    if not isinstance(value, list):
        return []
    return [bounded_text(item, max_chars=256) for item in value[:limit] if bounded_text(item, max_chars=256)]


def read_game_context_summary_from_payload(payload: dict[str, object]) -> dict[str, object]:
    return {field: payload[field] for field in GAME_CONTEXT_FIELDS if field in payload}


def read_game_context_summary(path: Path) -> dict[str, object]:
    return read_game_context_summary_from_payload(read_json_object(path))


def adapter_summary(adapter: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for field in ADAPTER_FIELDS:
        value = adapter.get(field)
        summary[field] = bounded_strings(value) if field == "levels" else value
    return summary


def workflow_summary(payload: dict[str, object]) -> dict[str, object]:
    health = payload.get("workflow_health", {})
    if not isinstance(health, dict):
        health = {}
    return {
        "generated_at": bounded_text(payload.get("generated_at", ""), max_chars=128),
        "project_state": bounded_text(payload.get("project_state", ""), max_chars=128),
        "readiness_overall_status": bounded_text(payload.get("readiness_overall_status", ""), max_chars=128),
        "workflow_health": {
            "verdict": bounded_text(health.get("verdict", ""), max_chars=128),
            "blocking_issues": health.get("blocking_issues", 0),
            "warnings": health.get("warnings", 0),
        },
    }


def task_summary(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("task_summary", {})
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in TASK_SUMMARY_FIELDS if field in value}


def action_summary(row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        return {}
    return {
        "mod": bounded_text(row.get("mod", ""), max_chars=128),
        "task_id": bounded_text(row.get("task_id", ""), max_chars=128),
        "command": bounded_text(row.get("command", "")),
        "type": bounded_text(row.get("type", ""), max_chars=64),
        "risk": bounded_text(row.get("risk", ""), max_chars=64),
        "can_run_parallel": bool(row.get("can_run_parallel", False)),
        "resource_locks": bounded_strings(row.get("resource_locks", "")),
        "must_read_evidence": bounded_strings(row.get("must_read_evidence", "")),
    }


def next_action_summaries(payload: dict[str, object]) -> list[dict[str, object]]:
    checkpoint = payload.get("resume_checkpoint", {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    actions = checkpoint.get("next_actions", payload.get("safe_next_actions", []))
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return []
    return [summary for row in actions[:MAX_NEXT_ACTIONS] if (summary := action_summary(row))]


def blocker_summaries(payload: dict[str, object]) -> list[dict[str, object]]:
    blockers = payload.get("blocking_mods", [])
    if not isinstance(blockers, list):
        return []
    result: list[dict[str, object]] = []
    for row in blockers[:MAX_BLOCKERS]:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "mod": bounded_text(row.get("mod", ""), max_chars=128),
                "state": bounded_text(row.get("state", ""), max_chars=128),
                "primary_blocker": bounded_text(row.get("primary_blocker", "")),
                "task_id": bounded_text(row.get("task_id", ""), max_chars=128),
                "can_run_parallel": bool(row.get("can_run_parallel", False)),
                "resource_locks": bounded_strings(row.get("resource_locks", "")),
                "handoff_target": bounded_text(row.get("handoff_target", ""), max_chars=64),
            }
        )
    return result


def checkpoint_summary(payload: dict[str, object]) -> dict[str, object]:
    checkpoint = payload.get("resume_checkpoint", {})
    if not isinstance(checkpoint, dict):
        return {}
    stale_rule = checkpoint.get("stale_if_newer_than", {})
    if not isinstance(stale_rule, dict):
        stale_rule = {}
    watch = stale_rule.get("watch", [])
    fingerprints: list[dict[str, str]] = []
    if isinstance(watch, list):
        for row in watch[:MAX_REFERENCES]:
            if not isinstance(row, dict):
                continue
            fingerprints.append(
                {
                    "path": bounded_text(row.get("path", ""), max_chars=256),
                    "fingerprint": bounded_text(row.get("fingerprint", ""), max_chars=128),
                }
            )
    return {
        "checkpoint_id": bounded_text(checkpoint.get("checkpoint_id", ""), max_chars=128),
        "generated_at_utc": bounded_text(checkpoint.get("generated_at_utc", ""), max_chars=128),
        "project_state": bounded_text(checkpoint.get("project_state", ""), max_chars=128),
        "readiness_overall_status": bounded_text(checkpoint.get("readiness_overall_status", ""), max_chars=128),
        "last_successful_stage": bounded_text(checkpoint.get("last_successful_stage", ""), max_chars=128),
        "next_read_set": bounded_strings(checkpoint.get("next_read_set", [])),
        "watch_fingerprints": fingerprints,
    }


def freshness_summary(freshness: dict[str, object]) -> dict[str, object]:
    reasons = freshness.get("reasons", [])
    reason_rows: list[dict[str, str]] = []
    if isinstance(reasons, list):
        for row in reasons[:MAX_REFERENCES]:
            if not isinstance(row, dict):
                continue
            reason_rows.append(
                {
                    "path": bounded_text(row.get("path", ""), max_chars=256),
                    "reason": bounded_text(row.get("reason", ""), max_chars=256),
                }
            )
    return {
        "checkpoint_id": bounded_text(freshness.get("checkpoint_id", ""), max_chars=128),
        "fresh": bool(freshness.get("fresh", False)),
        "status": bounded_text(freshness.get("status", ""), max_chars=64),
        "watch_count": freshness.get("watch_count", 0),
        "reasons": reason_rows,
    }


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

    handoff = read_json_object(handoff_path)
    game_context = read_game_context_summary_from_payload(handoff)
    freshness = (
        handoff_checkpoint_freshness(root, handoff_path)
        if handoff_path.name != "codex_handoff.json"
        else {"fresh": True, "status": "not_applicable", "reasons": []}
    )
    skills = skill_rows(args.agent)
    visible_skills = [bounded_text(row.get("skill_dir", ""), max_chars=128) for row in skills if row.get("usable")][
        :MAX_SKILLS
    ]
    gui_blocked = [
        bounded_text(row.get("skill_dir", ""), max_chars=128)
        for row in skills
        if row.get("requires_gui") and not row.get("usable")
    ][:MAX_SKILLS]

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
        "## Adapter Capabilities",
        "",
        "```json",
        json.dumps(adapter_summary(adapter), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Workflow Status",
        "",
        "```json",
        json.dumps(workflow_summary(handoff), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Task Summary",
        "",
        "```json",
        json.dumps(task_summary(handoff), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Blockers",
        "",
        "```json",
        json.dumps(blocker_summaries(handoff), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Next Actions",
        "",
        "```json",
        json.dumps(next_action_summaries(handoff), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Checkpoint Fingerprints",
        "",
        "```json",
        json.dumps(checkpoint_summary(handoff), ensure_ascii=False, indent=2),
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
        json.dumps(freshness_summary(freshness), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Usable Skills",
        "",
    ]
    for skill_dir in visible_skills:
        packet.append(f"- `{skill_dir}`")
    if gui_blocked:
        packet.extend(["", "## Codex-Only GUI Skills", ""])
        for skill_dir in gui_blocked:
            packet.append(f"- `{skill_dir}`")
    rendered = "\n".join(packet) + "\n"
    packet_bytes = rendered.encode("utf-8")
    if len(packet_bytes) > MAX_PACKET_BYTES:
        print(f"ERROR: bounded agent context exceeds hard limit: {len(packet_bytes)} > {MAX_PACKET_BYTES} bytes")
        return 1
    output.write_bytes(packet_bytes)
    print(f"Agent context written to: {relative_path(root, output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
