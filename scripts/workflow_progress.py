"""Emit user-facing workflow progress cards from workflow state evidence.

Progress is a deliberately small layer above qa/workflow_state.json. It writes
only workspace-local progress files and does not translate, extract, rebuild, or
touch Mod binaries.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from game_context import GAME_METADATA_KEYS, GameContext, game_context_metadata, game_display_label_from_metadata, game_metadata_mismatches
from project_paths import is_under, project_root, relative_path, resolve_project_path
from route_translation_task import current_game_context


USER_STAGES = [
    "workspace_ready",
    "input_discovered",
    "extracted",
    "routed",
    "candidates_extracted",
    "translated",
    "final_mod_built",
    "packaged",
    "qa_pending_strict",
    "qa_checked",
    "ready_for_manual_test",
]

PSEUDO_STAGE_ALIASES = {
    "blocked": ("workspace_ready", "blocked"),
    "qa_failed": ("qa_checked", "qa_failed"),
    "needs_input": ("workspace_ready", "needs_input"),
}

CLI_STAGE_CHOICES = [*USER_STAGES, *PSEUDO_STAGE_ALIASES.keys()]

QA_STAGE_TO_PROGRESS = {
    "needs_input": "workspace_ready",
    "discovered": "input_discovered",
    "extracted": "extracted",
    "routed": "routed",
    "candidates_extracted": "candidates_extracted",
    "translated": "translated",
    "tool_outputs_generated": "translated",
    "qa_failed": "qa_checked",
    "qa_passed": "qa_checked",
    "final_mod_built": "final_mod_built",
    "packaged": "packaged",
    "qa_pending_strict": "qa_pending_strict",
    "ready_for_manual_test": "ready_for_manual_test",
    "manual_tested": "ready_for_manual_test",
    "blocked": "workspace_ready",
}

STAGE_HEADLINES = {
    "workspace_ready": "工作区状态已确认",
    "input_discovered": "发现 Mod 输入",
    "extracted": "解包完成",
    "routed": "文件路由完成",
    "candidates_extracted": "玩家可见文本提取完成",
    "translated": "翻译候选生成完成",
    "qa_checked": "QA 门禁已检查",
    "final_mod_built": "final_mod 已组装",
    "packaged": "CHS 包已生成",
    "qa_pending_strict": "严格 QA 待运行",
    "ready_for_manual_test": "汉化产物已生成",
}

COMPLETED_LABELS = {
    "workspace_ready": "工作区检查",
    "input_discovered": "输入扫描",
    "extracted": "解包",
    "routed": "文件路由",
    "candidates_extracted": "文本提取",
    "translated": "翻译候选生成",
    "qa_checked": "QA 门禁",
    "final_mod_built": "final_mod 组装",
    "packaged": "CHS 打包",
    "qa_pending_strict": "严格 QA 准备",
    "ready_for_manual_test": "人工测试准备",
}

BLOCKING_STATUSES = {"blocked", "qa_failed", "needs_input", "error"}
DONE_STATUSES = {"done", "manual_tested"}
USER_PROGRESS_CARD_BEGIN = "SMT_PROGRESS_CARD_FOR_USER_BEGIN"
USER_PROGRESS_CARD_END = "SMT_PROGRESS_CARD_FOR_USER_END"


@dataclass
class ProgressPayload:
    run_id: str
    mod_name: str
    current_stage: str
    status: str
    headline: str
    summary: str
    next_action: str
    blockers: list[str]
    artifacts: list[str]
    completed_stages: list[str]
    source: str
    game_metadata: dict[str, object]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stage_number(stage: str) -> int:
    try:
        return USER_STAGES.index(stage) + 1
    except ValueError:
        return 1


def stage_at_or_before(stage: str) -> list[str]:
    index = stage_number(stage)
    return USER_STAGES[:index]


def prefix_for(status: str, blockers: list[str], stage: str) -> str:
    if status in BLOCKING_STATUSES or blockers:
        return "[SMT 阻断]"
    if status in DONE_STATUSES or stage == "ready_for_manual_test":
        return "[SMT 完成]"
    return "[SMT 进度]"


def normalize_status(status: str, blockers: list[str]) -> str:
    value = status.strip()
    if value == "manual_tested":
        return "done"
    if value == "ready_for_manual_test":
        return "done"
    if value == "qa_failed":
        return "qa_failed"
    if value == "needs_input":
        return "needs_input"
    if value == "blocked" or blockers:
        return "blocked"
    if value in {"ok", "running", "warning", "done", "error"}:
        return value
    return "ok" if value else "running"


def safe_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def safe_artifact_path(root: Path, value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        path = resolve_project_path(root, text, must_exist=False)
    except ValueError:
        return ""
    return relative_path(root, path)


def artifact_list_from_state(root: Path, state_row: dict[str, Any]) -> list[str]:
    artifacts: list[str] = []
    evidence = state_row.get("evidence", {})
    if isinstance(evidence, dict):
        for key in ("input", "workspace", "final_mod", "package"):
            value = str(evidence.get(key, "")).strip()
            artifact = safe_artifact_path(root, value)
            if artifact and artifact not in artifacts:
                artifacts.append(artifact)
    for path in (
        "qa/workflow_state.md",
        "qa/translation_readiness.md",
        "qa/workflow_health.md",
    ):
        if (root / path).is_file() and path not in artifacts:
            artifacts.append(path)
    return artifacts


def state_has_package_artifact(root: Path, state_row: dict[str, Any], blockers: list[str]) -> bool:
    if "chs_package_missing" in blockers:
        return False
    if any(str(blocker).startswith("package_validation") for blocker in blockers):
        return False
    evidence = state_row.get("evidence", {})
    package_validation = ""
    if isinstance(evidence, dict):
        package_validation = str(evidence.get("package_validation", "")).strip()
        if package_validation != "passed":
            return False
        package_value = str(evidence.get("package", "")).strip()
        package_path = safe_artifact_path(root, package_value)
        if package_path and (root / package_path).is_file():
            return True
    else:
        return False
    for artifact in artifact_list_from_state(root, state_row):
        if artifact.lower().endswith("_chs.zip") and (root / artifact).is_file():
            return True
    return False


def choose_primary_state(workflow_state: dict[str, Any]) -> dict[str, Any]:
    states = workflow_state.get("states", [])
    if not isinstance(states, list) or not states:
        return {}
    dict_states = [row for row in states if isinstance(row, dict)]
    if not dict_states:
        return {}
    for row in dict_states:
        blockers = safe_list(row.get("blocking_checks", []))
        if str(row.get("state", "")).strip() in {"qa_failed", "blocked", "needs_input"} or blockers:
            return row
    for row in dict_states:
        if str(row.get("state", "")).strip() not in {"ready_for_manual_test", "manual_tested"}:
            return row
    return dict_states[0]


def completed_from_state(state_row: dict[str, Any], progress_stage: str, status: str) -> list[str]:
    source_stage = str(state_row.get("last_success_stage", "")).strip()
    completed_stage = QA_STAGE_TO_PROGRESS.get(source_stage, "")
    if not completed_stage and status not in BLOCKING_STATUSES:
        completed_stage = progress_stage
    if not completed_stage:
        completed_stage = "workspace_ready"
    completed = stage_at_or_before(completed_stage)
    if status in {"ok", "done"} and progress_stage not in completed:
        completed = stage_at_or_before(progress_stage)
    return completed


def human_next_action(state_row: dict[str, Any], workflow_state: dict[str, Any]) -> str:
    if str(state_row.get("state", "")).strip() == "ready_for_manual_test":
        return "检查 final_mod / _CHS.zip，并按 qa/manual_game_test_plan.md 做游戏内人工测试。"
    for action in state_row.get("next_actions", []):
        if not isinstance(action, dict):
            continue
        command = str(action.get("command", "")).strip()
        reason = str(action.get("reason", "")).strip()
        if command:
            return command
        if reason:
            return reason
    command = str(state_row.get("next_command", "")).strip()
    if command:
        return command
    return str(workflow_state.get("next_command", "")).strip()


def summarize_state(workflow_state: dict[str, Any], state_row: dict[str, Any], status: str, stage: str) -> str:
    project_state = str(workflow_state.get("project_state", "")).strip()
    summary = workflow_state.get("state_summary", {})
    total = ready = blocking = in_progress = 0
    if isinstance(summary, dict):
        total = int(summary.get("total", 0) or 0)
        ready = int(summary.get("ready", 0) or 0)
        blocking = int(summary.get("blocking", 0) or 0)
        in_progress = int(summary.get("in_progress", 0) or 0)

    if status == "needs_input":
        return "工作区已初始化，当前没有可处理的 mod/ 输入。"
    if status == "qa_failed":
        return "QA 门禁未通过，流程已安全暂停，需先处理阻断报告。"
    if status == "blocked":
        return "流程已安全暂停，需先处理阻断项后再继续。"
    if stage == "ready_for_manual_test":
        return "项目内静态 QA 已通过，交付产物已生成；真实游戏/MO2/Vortex 验证尚未完成，需要玩家人工测试。"
    if total:
        return f"项目状态为 {project_state or 'unknown'}；共 {total} 个状态，ready {ready} 个，阻断 {blocking} 个，进行中 {in_progress} 个。"
    mod_name = str(state_row.get("mod", "")).strip()
    return f"{mod_name} 当前阶段为 {stage}。" if mod_name else f"当前阶段为 {stage}。"


def build_from_workflow_state(
    root: Path,
    workflow_state: dict[str, Any],
    context: GameContext | None = None,
    require_marker_match: bool = False,
) -> ProgressPayload:
    context = context or current_game_context(root)
    has_declared_metadata = any(key in workflow_state for key in GAME_METADATA_KEYS)
    if require_marker_match or has_declared_metadata:
        mismatches = game_metadata_mismatches(workflow_state, context, require_all=True)
        if mismatches:
            raise ValueError(f"workflow state game metadata mismatch: {'; '.join(mismatches)}")
    metadata = game_context_metadata(context)
    state_row = choose_primary_state(workflow_state)
    raw_state = str(state_row.get("state", workflow_state.get("project_state", ""))).strip()
    blockers = safe_list(state_row.get("blocking_checks", []))
    status = normalize_status(raw_state, blockers)
    progress_stage = QA_STAGE_TO_PROGRESS.get(raw_state, "")
    if not progress_stage:
        progress_stage = QA_STAGE_TO_PROGRESS.get(str(state_row.get("last_success_stage", "")).strip(), "workspace_ready")
    if raw_state == "blocked":
        last_success_stage = str(state_row.get("last_success_stage", "")).strip()
        progress_stage = QA_STAGE_TO_PROGRESS.get(last_success_stage, progress_stage)
    if raw_state == "final_mod_built" and state_has_package_artifact(root, state_row, blockers):
        progress_stage = "packaged"
    headline = STAGE_HEADLINES.get(progress_stage, "工作流状态已更新")
    if status == "needs_input":
        headline = "需要放入 Mod 输入"
    elif status == "blocked":
        headline = "流程已安全暂停"
    elif status == "qa_failed":
        headline = "QA 门禁未通过"
    elif progress_stage == "ready_for_manual_test":
        headline = "汉化产物已生成"

    existing = read_json(root / ".workflow" / "workflow_state.json")
    run_id = str(os.environ.get("SKYRIM_CHS_RUN_ID") or existing.get("run_id") or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    return ProgressPayload(
        run_id=run_id,
        mod_name=str(state_row.get("mod", "")).strip(),
        current_stage=progress_stage,
        status=status,
        headline=headline,
        summary=summarize_state(workflow_state, state_row, status, progress_stage),
        next_action=human_next_action(state_row, workflow_state),
        blockers=blockers,
        artifacts=artifact_list_from_state(root, state_row),
        completed_stages=completed_from_state(state_row, progress_stage, status),
        source="qa/workflow_state.json",
        game_metadata=metadata,
    )


def build_manual_payload(
    root: Path,
    *,
    stage: str,
    status: str,
    headline: str,
    summary: str,
    next_action: str,
    mod_name: str,
    blockers: list[str],
    artifacts: list[str],
) -> ProgressPayload:
    if stage in PSEUDO_STAGE_ALIASES:
        stage, status = PSEUDO_STAGE_ALIASES[stage]
    if stage not in USER_STAGES:
        raise ValueError(f"Unknown progress stage: {stage}")
    normalized_status = normalize_status(status, blockers)
    existing = read_json(root / ".workflow" / "workflow_state.json")
    run_id = str(os.environ.get("SKYRIM_CHS_RUN_ID") or existing.get("run_id") or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    completed = stage_at_or_before(stage) if normalized_status in {"ok", "done"} else stage_at_or_before(USER_STAGES[max(0, stage_number(stage) - 2)])
    return ProgressPayload(
        run_id=run_id,
        mod_name=mod_name,
        current_stage=stage,
        status=normalized_status,
        headline=headline or STAGE_HEADLINES.get(stage, "工作流状态已更新"),
        summary=summary,
        next_action=next_action,
        blockers=blockers,
        artifacts=artifacts,
        completed_stages=completed,
        source="manual_emit",
        game_metadata=game_context_metadata(current_game_context(root)),
    )


def should_append_event(root: Path, card_payload: dict[str, Any]) -> bool:
    previous = read_json(root / ".workflow" / "progress_card.json")
    keys = ("prefix", "headline", "stage", "status", "summary", "next_action", "blockers", "game_id")
    return any(previous.get(key) != card_payload.get(key) for key in keys)


def write_progress(root: Path, progress: ProgressPayload) -> dict[str, Any]:
    workflow_dir = resolve_project_path(root, ".workflow", must_exist=False)
    traces_dir = resolve_project_path(root, "traces", must_exist=False)
    qa_dir = resolve_project_path(root, "qa", must_exist=False)
    for directory in (workflow_dir, traces_dir, qa_dir):
        if not is_under(directory, root):
            raise ValueError(f"Progress output directory escaped workspace: {directory}")
        directory.mkdir(parents=True, exist_ok=True)

    now = utc_now()
    prefix = prefix_for(progress.status, progress.blockers, progress.current_stage)
    stage_index = stage_number(progress.current_stage)
    artifacts: list[str] = []
    for value in progress.artifacts:
        artifact = safe_artifact_path(root, str(value))
        if artifact and artifact not in artifacts:
            artifacts.append(artifact)
    workflow_payload = {
        **progress.game_metadata,
        "schema_version": 1,
        "run_id": progress.run_id,
        "mod_name": progress.mod_name,
        "current_stage": progress.current_stage,
        "stage_index": stage_index,
        "stage_total": len(USER_STAGES),
        "status": progress.status,
        "completed_stages": progress.completed_stages,
        "next_action": progress.next_action,
        "blockers": progress.blockers,
        "artifacts": artifacts,
        "source": progress.source,
        "last_updated": now,
    }
    card_payload = {
        **progress.game_metadata,
        "kind": "progress",
        "prefix": prefix,
        "headline": progress.headline,
        "stage": progress.current_stage,
        "stage_index": stage_index,
        "stage_total": len(USER_STAGES),
        "status": progress.status,
        "completed_stages": progress.completed_stages,
        "summary": progress.summary,
        "next_action": progress.next_action,
        "artifacts": artifacts,
        "blockers": progress.blockers,
        "updated_at": now,
        "game_label": game_display_label_from_metadata(progress.game_metadata),
    }
    append_event = should_append_event(root, card_payload)
    write_json(workflow_dir / "workflow_state.json", workflow_payload)
    write_json(workflow_dir / "progress_card.json", card_payload)
    (workflow_dir / "progress_card.md").write_text(render_progress_card(card_payload), encoding="utf-8")
    if append_event:
        event = {
            **progress.game_metadata,
            "time": now,
            "run_id": progress.run_id,
            "mod_name": progress.mod_name,
            "stage": progress.current_stage,
            "status": progress.status,
            "headline": progress.headline,
            "prefix": prefix,
            "blockers": progress.blockers,
        }
        with (workflow_dir / "progress_events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    write_timeline(root)
    write_blockers(root, card_payload)
    return card_payload


def render_progress_card(card: dict[str, Any]) -> str:
    artifacts = card.get("artifacts", [])
    blockers = card.get("blockers", [])
    completed_text = ""
    stage = str(card.get("stage", ""))
    completed_stages = safe_list(card.get("completed_stages", []))
    if not completed_stages and stage in USER_STAGES:
        completed_stages = stage_at_or_before(stage)
    if completed_stages:
        completed_text = "、".join(COMPLETED_LABELS[item] for item in completed_stages if item in COMPLETED_LABELS)
    detail = "、".join(str(item) for item in artifacts[:4]) if isinstance(artifacts, list) and artifacts else "无"
    blocker_text = "、".join(str(item) for item in blockers) if isinstance(blockers, list) and blockers else "无"
    prefix = str(card.get("prefix", "[SMT 进度]"))
    lines = [
        f"## {prefix} {card.get('stage_index', 0)}/{card.get('stage_total', 0)} {card.get('headline', '')}",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 当前状态 | `{card.get('stage', '')}` / `{card.get('status', '')}` |",
        f"| 当前游戏 | {card.get('game_label', card.get('game_display_name', ''))} / `{card.get('support_level', '')}` |",
        f"| 已完成 | {completed_text or '无'} |",
        f"| 当前摘要 | {card.get('summary', '')} |",
        f"| 阻断项 | {blocker_text} |",
        f"| 下一步 | {card.get('next_action', '') or '无'} |",
        f"| 详细记录 | {detail} |",
    ]
    return "\n".join(lines) + "\n"


def read_progress_card_markdown(root: Path) -> str:
    path = resolve_project_path(root, ".workflow/progress_card.md", must_exist=False)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def print_progress_card_for_user(root: Path) -> None:
    text = read_progress_card_markdown(root)
    if not text:
        print("SMT progress card: .workflow/progress_card.md is missing or empty.")
        return
    print("")
    print("SMT progress card for controller agent: after workflow/QA/state refresh, re-read .workflow/progress_card.md and present it directly as rendered Markdown. Do not wrap it in triple backticks, a code block, or a quote block. Do not rely on stdout or a summary.")
    print(USER_PROGRESS_CARD_BEGIN)
    print(text)
    print(USER_PROGRESS_CARD_END)


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_timeline(root: Path) -> None:
    rows = read_jsonl(root / ".workflow" / "progress_events.jsonl")
    path = root / "qa" / "workflow_timeline.md"
    lines = [
        "# Workflow Timeline",
        "",
        "| Time | Prefix | Stage | Status | Headline | Mod |",
        "|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| | | | | No progress events recorded. | |")
    else:
        for row in rows:
            lines.append(
                f"| {markdown_cell(row.get('time', ''))} | {markdown_cell(row.get('prefix', ''))} | "
                f"{markdown_cell(row.get('stage', ''))} | {markdown_cell(row.get('status', ''))} | "
                f"{markdown_cell(row.get('headline', ''))} | {markdown_cell(row.get('mod_name', ''))} |"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_blockers(root: Path, card: dict[str, Any]) -> None:
    path = root / "qa" / "blockers.md"
    blockers = safe_list(card.get("blockers", []))
    status = str(card.get("status", "")).strip()
    lines = ["# Blockers", ""]
    if status in BLOCKING_STATUSES or blockers:
        lines.extend(
            [
                f"## 当前状态：{status or 'blocked'}",
                "",
                f"- 阻断摘要：{card.get('summary', '')}",
                f"- 下一步：{card.get('next_action', '') or '无'}",
                "",
                "## 阻断项",
                "",
            ]
        )
        if blockers:
            lines.extend(f"- {blocker}" for blocker in blockers)
        else:
            lines.append("- needs_input")
    else:
        lines.append("当前无阻断项。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def emit_from_qa_workflow_state(root: Path, workflow_state: dict[str, Any]) -> dict[str, Any]:
    context = current_game_context(root)
    return write_progress(
        root,
        build_from_workflow_state(root, workflow_state, context, require_marker_match=True),
    )


def emit_from_state_file(root: Path, state_path: Path) -> dict[str, Any]:
    context = current_game_context(root)
    payload = read_json(state_path)
    if not payload:
        payload = {
            **game_context_metadata(context),
            "project_state": "needs_input",
            "states": [
                {
                    "mod": "",
                    "state": "needs_input",
                    "last_success_stage": "",
                    "blocking_checks": ["workflow_state_missing"],
                    "next_command": "python scripts/audit_translation_readiness.py",
                    "evidence": {},
                }
            ],
        }
    return write_progress(
        root,
        build_from_workflow_state(root, payload, context, require_marker_match=True),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write .workflow progress card files from workflow state evidence.")
    sub = parser.add_subparsers(dest="command", required=True)

    from_state = sub.add_parser("from-state", help="derive progress from qa/workflow_state.json")
    from_state.add_argument("--workflow-state-path", default="qa/workflow_state.json")

    emit = sub.add_parser("emit", help="emit an explicit progress event")
    emit.add_argument("--stage", required=True, choices=CLI_STAGE_CHOICES)
    emit.add_argument("--status", required=True, choices=["running", "ok", "warning", "blocked", "qa_failed", "needs_input", "done", "error"])
    emit.add_argument("--headline", default="")
    emit.add_argument("--summary", default="")
    emit.add_argument("--next", dest="next_action", default="")
    emit.add_argument("--mod-name", default="")
    emit.add_argument("--artifact", action="append", default=[])
    emit.add_argument("--blocker", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = project_root()
    if args.command == "from-state":
        state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
        card = emit_from_state_file(root, state_path)
    else:
        card = write_progress(
            root,
            build_manual_payload(
                root,
                stage=args.stage,
                status=args.status,
                headline=args.headline,
                summary=args.summary,
                next_action=args.next_action,
                mod_name=args.mod_name,
                blockers=[str(item) for item in args.blocker],
                artifacts=[str(item) for item in args.artifact],
            ),
        )
    print(f"{card.get('prefix', '[SMT 进度]')} {card.get('stage_index', 0)}/{card.get('stage_total', 0)} {card.get('headline', '')}")
    print("Progress card written to: .workflow/progress_card.md")
    print_progress_card_for_user(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
