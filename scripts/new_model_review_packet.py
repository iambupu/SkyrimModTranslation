"""Create an agent model-review prompt packet from translation intermediates."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from game_context import GameContext, game_display_label
from model_review_contract import model_claim_lines
from project_paths import project_root
from route_translation_task import current_game_context
from translation_context import (
    append_review_group_sections,
    append_review_groups_table,
    aggregate_review_rows,
    validated_translation_context,
    write_translation_context_packet,
)
from update_model_review_contract import build_contract_block
from project_paths import is_under, resolve_project_path, relative_posix_path as relative_path
from report_utils import markdown_cell
from translation_text import row_value as json_value_any


SOURCE_FIELDS = ("Source", "source", "original", "Original", "text", "Text")
TARGET_FIELDS = ("Result", "result", "Target", "target", "translation", "Dest", "dest")
RISK_FIELDS = ("risk", "Risk")
TYPE_FIELDS = ("Type", "type", "Kind", "kind", "category", "record_type", "RecordType")
CONTEXT_FIELDS = (
    "Context",
    "context",
    "function_name",
    "editor_id",
    "EditorID",
    "subrecord_type",
    "SubrecordType",
    "reason",
    "notes",
    "callee",
    "opcode_form",
    "semantic_argument_index",
    "semantic_argument_role",
    "visibility_basis",
    "classification",
    "is_direct_literal",
)









def read_input_list(root: Path, input_paths: list[str], input_list_path: str) -> list[str]:
    effective = [item for item in input_paths if item.strip()]
    if input_list_path.strip():
        list_path = resolve_project_path(root, input_list_path, must_exist=True)
        for line in list_path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if stripped:
                effective.append(stripped)
    if not effective:
        raise ValueError("At least one --input-path or --input-list-path entry is required.")
    return effective


def iter_json_files(root: Path, input_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in input_paths:
        path = resolve_project_path(root, value, must_exist=True)
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".jsonl", ".json"}))
        else:
            files.append(path)
    return files


def collect_rows(root: Path, files: list[Path], include_protected_rows: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file in files:
        line_number = 0
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            line_number += 1
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            risk = json_value_any(row, *RISK_FIELDS)
            source = json_value_any(row, *SOURCE_FIELDS)
            target = json_value_any(row, *TARGET_FIELDS)
            semantic_classification = json_value_any(
                row,
                "classification",
                "Classification",
            ).strip()
            if (
                not include_protected_rows
                and semantic_classification in {"protected", "manual_review"}
                and not target.strip()
            ):
                continue
            if not include_protected_rows and risk.lower() in {"protected", "protected-logic"} and not target.strip():
                continue
            if not source.strip() and not target.strip():
                continue
            context_values: list[str] = []
            for field in CONTEXT_FIELDS:
                value = json_value_any(row, field)
                if value.strip():
                    context_values.append(f"{field}={value}")
            rows.append(
                {
                    "File": relative_path(root, file),
                    "Line": line_number,
                    "Type": json_value_any(row, *TYPE_FIELDS),
                    "Risk": risk,
                    "Context": "; ".join(context_values),
                    "Source": source,
                    "Target": target,
                }
            )
    return rows


def write_packet(
    root: Path,
    mod_name: str,
    output_path: Path,
    review_path: Path,
    rows: list[dict[str, object]],
    *,
    game_context: GameContext | None = None,
    context_path: Path | None = None,
    context_payload: dict[str, object] | None = None,
    context_source_hash: str = "",
) -> None:
    groups = aggregate_review_rows(rows)
    context_payload = context_payload or {}
    context_status = str(context_payload.get("status", "missing")).strip() or "missing"
    lines = [
        f"# Model Review Packet: {mod_name}",
        "",
        f"- Created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Game: {game_display_label(game_context)}" if game_context else "- Game: current workspace Game Profile",
        f"- Rows for agent model review: {len(rows)}",
        f"- Aggregated review groups: {len(groups)}",
        f"- Review output: {relative_path(root, review_path)}",
        f"- Mod context: {relative_path(root, context_path)}" if context_path else "- Mod context: missing",
        f"- Mod context status: {context_status}",
        f"- Mod context source SHA256: {context_source_hash}",
        "",
        "## Mod Translation Context",
        "",
    ]
    if context_status == "complete":
        lines.extend(
            [
                f"- Summary: {context_payload.get('summary', '')}",
                f"- Purpose: {context_payload.get('purpose', '')}",
                f"- Features: {'; '.join(str(item) for item in context_payload.get('features', []) if str(item).strip())}",
                f"- Tone: {context_payload.get('tone', '')}",
                f"- Confidence: {context_payload.get('confidence', '')}",
            ]
        )
        term_preferences = context_payload.get("term_preferences", [])
        ui_rules = context_payload.get("ui_label_rules", [])
        ambiguous = context_payload.get("ambiguous_terms", [])
        if term_preferences:
            lines.append(f"- Term preferences: {json.dumps(term_preferences, ensure_ascii=False, separators=(',', ':'))}")
        if ui_rules:
            lines.append(f"- UI label rules: {json.dumps(ui_rules, ensure_ascii=False, separators=(',', ':'))}")
        if ambiguous:
            lines.append(f"- Ambiguous terms: {json.dumps(ambiguous, ensure_ascii=False, separators=(',', ':'))}")
    else:
        lines.append("The Mod context is missing, incomplete, or stale. Analyze the context packet and complete it before a semantic PASS.")
    lines.extend(
        [
            "",
            "## Review Instructions",
            "",
            "The reviewing agent must use model judgment here. Do not treat regex/script checks as semantic proof.",
            "",
            "Check:",
            "",
            "- Whether the Chinese is natural Simplified Chinese game localization.",
            "- Whether UI/MCM text is short, clear, and not wordy.",
            "- Whether terminology, tone, and world context fit the current Game Profile and evidence-bound Mod summary; do not impose an unsupported genre or setting.",
            "- Whether subjects, objects, actions, control ownership, and functional relationships remain complete instead of being translated word by word.",
            "- Whether short labels are understandable with their related help text and use the same terminology.",
            "- Whether anything protected was translated by mistake.",
            "- Whether English should intentionally remain, such as mod/tool names, plugin names, acronyms, or filenames.",
            "- Whether concatenated PEX fragments still read naturally when combined.",
            "- For Fallout 4 PEX, only `classification=visible` with a direct literal and registry `visibility_basis` is writable; never promote protected/manual-review rows from wording or context alone.",
            "",
            "The review output must include these exact final claims when the review passes:",
            "",
            *model_claim_lines(code=True),
            "",
            "Write findings to the review output with severity, file, line, issue, and proposed target.",
            "",
            "## Aggregated Rows",
            "",
            "Only rows with the same source, target, type, risk, and context are compressed. Each conclusion names a review group ID and covers all of that group's occurrence references; occurrence-specific exceptions must name the file and line.",
            "",
        ]
    )
    append_review_groups_table(
        lines,
        groups,
        kind_heading="Type",
        target_heading="Target",
        include_line=True,
        cell=markdown_cell,
    )
    append_review_group_sections(lines, groups)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_template(root: Path, mod_name: str, output_path: Path, review_path: Path) -> None:
    if review_path.exists():
        return
    review = [
        f"# Model Translation Review: {mod_name}",
        "",
        "- Reviewed at: TODO",
        "- Reviewer: Agent model",
        f"- Packet: {relative_path(root, output_path)}",
        "",
        build_contract_block(root, mod_name),
        "",
        "## Verdict",
        "",
        "TODO",
        "",
        "Required final claims when passing:",
        "",
        *model_claim_lines(),
        "",
        "## Findings",
        "",
        "| Severity | File | Line | Issue | Proposed target |",
        "|---|---|---:|---|---|",
        "",
        "## Notes",
        "",
        "TODO",
    ]
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(review) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an agent model review packet from project-local JSONL translation files.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--input-path", action="append", default=[])
    parser.add_argument("--input-list-path", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--review-output-path", default="")
    parser.add_argument("--context-packet-path", default="")
    parser.add_argument("--context-output-path", default="")
    parser.add_argument("--include-protected-rows", action="store_true")
    args = parser.parse_args()

    root = project_root()
    output_path = resolve_project_path(root, args.output_path or f"qa/{args.mod_name}.model_review_packet.md", must_exist=False)
    review_path = resolve_project_path(root, args.review_output_path or f"qa/{args.mod_name}.model_review.md", must_exist=False)
    context_packet_path = resolve_project_path(
        root,
        args.context_packet_path or f"qa/{args.mod_name}.translation_context_packet.md",
        must_exist=False,
    )
    context_path = resolve_project_path(
        root,
        args.context_output_path or f"qa/{args.mod_name}.translation_context.json",
        must_exist=False,
    )
    if not all(is_under(path, root / "qa") for path in (output_path, review_path, context_packet_path, context_path)):
        raise ValueError("Output paths must be under qa/.")

    input_paths = read_input_list(root, args.input_path, args.input_list_path)
    files = iter_json_files(root, input_paths)
    rows = collect_rows(root, files, args.include_protected_rows)
    game_context = current_game_context(root)
    context_source_hash, _ = write_translation_context_packet(
        root,
        args.mod_name,
        rows,
        game_context,
        context_packet_path,
        context_path,
    )
    context_payload, context_issues = validated_translation_context(root, args.mod_name, game_context)
    write_packet(
        root,
        args.mod_name,
        output_path,
        review_path,
        rows,
        game_context=game_context,
        context_path=context_path,
        context_payload=context_payload,
        context_source_hash=context_source_hash,
    )
    write_review_template(root, args.mod_name, output_path, review_path)

    print(f"Model review packet written to: {output_path}")
    print(f"Model review output path: {review_path}")
    print(f"Mod translation context packet: {context_packet_path}")
    print(f"Mod translation context: {context_path}")
    print(f"Mod translation context issues: {len(context_issues)}")
    print(f"Rows: {len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Model review packet failed: {exc}", file=sys.stderr)
        sys.exit(1)
