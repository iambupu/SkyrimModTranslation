"""Refresh the machine-checkable evidence block in an agent model review.

This script does not perform semantic review. It preserves the human/model
verdict and findings, and only updates the current final_mod packet hashes,
final review quality rows, reviewed file list, and required contract claims.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from file_utils import read_json_object_or_empty_with_parse_errors as read_json
from file_utils import read_text_utf8_sig_strict as read_text
from model_review_contract import (
    MODEL_REVIEWER_RE,
    jsonl_file_values,
    model_claim_lines,
    read_report_metric,
)
from project_paths import project_root
from project_paths import is_under, resolve_project_path, relative_windows_path as relative_path


BEGIN_MARKER = "<!-- BEGIN MODEL REVIEW CONTRACT -->"
END_MARKER = "<!-- END MODEL REVIEW CONTRACT -->"






def default_review_text(mod_name: str) -> str:
    return "\n".join(
        [
            f"# Model Translation Review: {mod_name}",
            "",
            "- Reviewed at: TODO",
            "- Reviewer: Agent model",
            "- Verdict: TODO",
            "",
            "## Findings",
            "",
            "TODO: An agent model must review the current final_mod text and binary packets before PASS is allowed.",
            "",
            "## Notes",
            "",
            "TODO: Summarize semantic review scope and any intentional retained English.",
            "",
        ]
    )


def build_contract_block(root: Path, mod_name: str) -> str:
    text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    text_items = root / "qa" / f"{mod_name}.final_text_review_items.jsonl"
    binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    binary_items = root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"
    quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
    quality_json = root / "qa" / f"{mod_name}.final_review_quality.json"

    rows_checked = str(read_json(quality_json).get("RowsChecked") or read_report_metric(quality_report, "Rows checked"))
    reviewed_files = sorted(jsonl_file_values(text_items, "File") | jsonl_file_values(binary_items, "File"))

    lines = [
        BEGIN_MARKER,
        "",
        "## Current Review Contract",
        "",
        f"- Contract refreshed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Text packet: {relative_path(root, text_packet)}",
        f"- Text items: {relative_path(root, text_items)}",
        f"- Text Items SHA256: {read_report_metric(text_packet, 'Items SHA256')}",
        f"- Binary packet: {relative_path(root, binary_packet)}",
        f"- Binary items: {relative_path(root, binary_items)}",
        f"- Binary Items SHA256: {read_report_metric(binary_packet, 'Items SHA256')}",
        f"- Final quality report: {relative_path(root, quality_report)}",
        f"- Final quality RowsChecked: {rows_checked}",
        "",
        "Required passing claims:",
        "",
        *model_claim_lines(),
        "",
        "Reviewed files:",
        "",
    ]
    if reviewed_files:
        lines.extend(f"- {item}" for item in reviewed_files)
    else:
        lines.append("- none")
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def replace_contract(text: str, block: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}\n?", re.DOTALL)
    if pattern.search(text):
        # Use a replacement callback so Windows paths in the block are not parsed as regex escapes.
        return pattern.sub(lambda _match: "\n\n" + block + "\n\n", text).strip() + "\n"
    lines = text.rstrip().splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = index + 1
            break
    lines[insert_at:insert_at] = ["", block, ""]
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh current packet evidence in qa/<ModName>.model_review.md.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--model-review-path", default="")
    parser.add_argument("--create-if-missing", action="store_true")
    args = parser.parse_args()

    root = project_root()
    review_path = resolve_project_path(root, args.model_review_path or f"qa/{args.mod_name}.model_review.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(review_path, qa_root):
        raise ValueError("ModelReviewPath must be under qa/.")

    if review_path.is_file():
        text = read_text(review_path)
    elif args.create_if_missing:
        text = default_review_text(args.mod_name)
    else:
        raise FileNotFoundError(f"missing model review: {relative_path(root, review_path)}")

    if MODEL_REVIEWER_RE.search(text) is None:
        raise ValueError("Existing model review must state 'Reviewer: Agent model' or legacy 'Reviewer: Codex model'.")

    updated = replace_contract(text, build_contract_block(root, args.mod_name))
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(updated, encoding="utf-8")
    print(f"Model review contract refreshed: {review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
