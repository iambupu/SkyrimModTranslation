"""Generate a player-operated runtime checklist from ready CHS outputs."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from game_context import GameContext, game_context_metadata, game_display_label
from model_review_contract import read_jsonl_objects as read_jsonl
from project_paths import project_root
from report_utils import markdown_cell
from route_translation_task import current_game_context
from file_utils import read_json_object_or_empty as read_json


@dataclass
class GameTestRow:
    ModName: str
    PackagePath: str
    FinalModDir: str
    ChangedFiles: list[str]
    RepresentativeTexts: list[str]
    RequiredChecks: list[str]
    Status: str



def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def review_rows(root: Path, mod_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(read_jsonl(root / "qa" / f"{mod_name}.final_text_review_items.jsonl"))
    rows.extend(read_jsonl(root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"))
    return rows


def required_checks(mod_name: str, rows: list[dict[str, Any]], context: GameContext) -> list[str]:
    game_label = game_display_label(context)
    checks = [
        "Player installs the CHS package as a separate local MO2/Vortex mod and enables only the required dependencies plus this output.",
        f"Player launches {game_label} and confirms the main menu/load process reaches an in-game save without plugin load errors.",
        "Player opens the mod manager plugin list and confirms the translated plugin is enabled with no missing master warning.",
        "Player inspects the visible in-game text listed in Representative Texts and confirms it is Chinese, natural, and not truncated.",
        "Player plays for several minutes in the affected cell/UI path and confirms no crash, infinite loading, broken menu, or missing asset appears.",
    ]
    changed_files = {str(row.get("File", "")).lower() for row in rows}
    if any("mcm" in file_name or "interface/translations" in file_name.replace("\\", "/") for file_name in changed_files):
        checks.extend(
            [
                "Player opens the translated MCM or interface surface and inspects all pages, options, prompts, and help text.",
                "Player triggers the translated confirmation/message text at least once and confirms button labels and placeholders render correctly.",
            ]
        )
    if any(file_name.endswith(".esp") for file_name in changed_files):
        checks.extend(
            [
                "Player opens the relevant inventory/spell/effect/location surfaces and confirms all translated ESP FULL/DESC text is visible.",
                "If this is a follower mod, player visits the follower's expected location and confirms the follower loads, can be interacted with, and uses the translated names/effects.",
            ]
        )
    if any(file_name.endswith(".pex") for file_name in changed_files):
        checks.append("Player triggers the script-driven feature that owns the translated PEX strings and confirms no Papyrus error appears in normal play.")
    return checks


def build_row(root: Path, output: dict[str, Any], context: GameContext) -> GameTestRow:
    mod_name = str(output.get("ModName", ""))
    rows = review_rows(root, mod_name)
    changed_files = unique_ordered([str(row.get("File", "")) for row in rows if row.get("File")])
    representative = unique_ordered([str(row.get("Final", "")) for row in rows if row.get("Final")])[:30]
    return GameTestRow(
        ModName=mod_name,
        PackagePath=str(output.get("PackagedModPath", "")),
        FinalModDir=str(output.get("FinalModDir", "")),
        ChangedFiles=changed_files,
        RepresentativeTexts=representative,
        RequiredChecks=required_checks(mod_name, rows, context),
        Status="pending_manual_game_test",
    )


def write_reports(
    root: Path,
    report_path: Path,
    json_path: Path,
    rows: list[GameTestRow],
    context: GameContext,
) -> None:
    game_label = game_display_label(context)
    lines = [
        "# Player-Operated Game Test Plan",
        "",
        f"- ProjectRoot: {root}",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Game Profile: {game_label}",
        f"- Mods to test: {len(rows)}",
        "- Status: pending player-operated game/MO2/Vortex validation",
        "",
        "## Purpose",
        "",
        "This plan covers the remaining runtime-risk validation that cannot be proven by project-local static QA alone.",
        f"The player performs these checks one CHS package at a time in a real {game_label} profile, outside this automation flow.",
        "Agent must not operate the real game, MO2, Vortex, Steam, AppData, or Documents/My Games paths; the workflow only validates the player-provided project-local evidence afterward.",
        "",
        "## Summary",
        "",
        "| ModName | CHS package | final_mod | Changed files | Status |",
        "|---|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {markdown_cell(row.ModName)} | {markdown_cell(row.PackagePath)} | {markdown_cell(row.FinalModDir)} | "
            f"{len(row.ChangedFiles)} | {row.Status} |"
        )

    for row in rows:
        lines.extend(
            [
                "",
                f"## {row.ModName}",
                "",
                f"- CHS package: `{row.PackagePath}`",
                f"- final_mod: `{row.FinalModDir}`",
                f"- Status: {row.Status}",
                "",
                "### Changed Files",
                "",
            ]
        )
        lines.extend([f"- `{item}`" for item in row.ChangedFiles] or ["- No changed files listed in review packets."])
        lines.extend(["", "### Representative Texts", ""])
        lines.extend([f"- {item}" for item in row.RepresentativeTexts] or ["- No representative text rows found."])
        lines.extend(["", "### Required Manual Checks", ""])
        lines.extend([f"- [ ] {item}" for item in row.RequiredChecks])

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This file is a player-operated checklist only.",
            "- The generator reads project-local QA reports and does not access the real game, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
            "- Agent must not perform these runtime checks directly; the player performs them and records evidence under qa/manual_game_test_artifacts/.",
            "- Do not mark a Mod as runtime-tested until the player has performed these checks in a real game profile.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "ProjectRoot": str(root),
                "GeneratedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Status": "pending_manual_game_test",
                **game_context_metadata(context),
                "Rows": [asdict(row) for row in rows],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a profile-aware player-operated in-game validation checklist.")
    parser.add_argument("--report-output-path", default="qa/manual_game_test_plan.md")
    parser.add_argument("--json-output-path", default="qa/manual_game_test_plan.json")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
    readiness = read_json(root / "qa" / "translation_readiness.json")
    outputs = readiness.get("KnownModOutputs", [])
    if not isinstance(outputs, list):
        outputs = []
    rows = [
        build_row(root, output, context)
        for output in outputs
        if isinstance(output, dict) and output.get("OverallStatus") == "ready_for_manual_test"
    ]
    write_reports(root, root / args.report_output_path, root / args.json_output_path, rows, context)
    print(f"Player-operated game test plan written to: {root / args.report_output_path}")
    print(f"Player-operated game test plan JSON written to: {root / args.json_output_path}")
    print(f"Mods to test: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
