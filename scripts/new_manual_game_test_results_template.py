from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import project_root, relative_path, resolve_project_path


@dataclass
class CheckResult:
    Name: str
    Status: str
    Evidence: str
    EvidenceArtifacts: list[str]
    Notes: str


@dataclass
class RuntimeResultRow:
    ModName: str
    Status: str
    CheckedAt: str
    Tester: str
    PackagePath: str
    PackageSha256: str
    FinalModDir: str
    FinalManifestSha256: str
    TestEnvironment: dict[str, str]
    CheckResults: list[CheckResult]
    RuntimeIssues: str
    Notes: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_row(root: Path, source: dict[str, Any]) -> RuntimeResultRow:
    package_rel = str(source.get("PackagePath", ""))
    final_rel = str(source.get("FinalModDir", ""))
    package_path = resolve_project_path(root, package_rel, must_exist=True)
    final_mod_dir = resolve_project_path(root, final_rel, must_exist=True)
    manifest_path = final_mod_dir / "meta" / "manifest.json"
    required_checks = source.get("RequiredChecks", [])
    if not isinstance(required_checks, list):
        required_checks = []
    return RuntimeResultRow(
        ModName=str(source.get("ModName", "")),
        Status="pending",
        CheckedAt="",
        Tester="",
        PackagePath=relative_path(root, package_path),
        PackageSha256=sha256_file(package_path),
        FinalModDir=relative_path(root, final_mod_dir),
        FinalManifestSha256=sha256_file(manifest_path) if manifest_path.is_file() else "",
        TestEnvironment={
            "Game": "Skyrim SE/AE",
            "GameVersion": "",
            "ModManager": "",
            "Profile": "",
            "LoadOrderNotes": "",
        },
        CheckResults=[CheckResult(str(item), "pending", "", [], "") for item in required_checks],
        RuntimeIssues="",
        Notes="",
    )


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_markdown(root: Path, path: Path, rows: list[RuntimeResultRow]) -> None:
    lines = [
        "# Player-Operated Game Test Results Template",
        "",
        f"- ProjectRoot: {root}",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mods: {len(rows)}",
        "",
        "The player fills `qa/manual_game_test_results.json` from this JSON template after operating the real Skyrim SE/AE profile. Codex must not perform the runtime checks directly.",
        "Do not mark a row passed until the player has performed every check in a real Skyrim SE/AE profile and saved project-local evidence.",
        "",
        "| ModName | Package | Checks | Status |",
        "|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(f"| {markdown_cell(row.ModName)} | {markdown_cell(row.PackagePath)} | {len(row.CheckResults)} | {row.Status} |")
    lines.extend(
        [
            "",
            "## Required Evidence",
            "",
            "- Keep `PackageSha256` and `FinalManifestSha256` unchanged from the template unless the output was rebuilt.",
            "- The player sets every `CheckResults[].Status` to `passed` and fills `Evidence` with observed in-game proof.",
            "- The player puts screenshots, logs, or load-order notes under `qa/manual_game_test_artifacts/<ModName>/` and lists them in `CheckResults[].EvidenceArtifacts`.",
            "- Set `RuntimeIssues` to `none` only after the player confirms there are no plugin load errors, crashes, broken UI paths, missing assets, or Papyrus/runtime issues relevant to the changed text.",
            "- After filling results, run `python .\\scripts\\validate_manual_game_test_results.py`.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a fillable player-operated Skyrim runtime test result template from the current manual game test plan.")
    parser.add_argument("--plan-json-path", default="qa/manual_game_test_plan.json")
    parser.add_argument("--json-output-path", default="qa/manual_game_test_results.template.json")
    parser.add_argument("--report-output-path", default="qa/manual_game_test_results_template.md")
    args = parser.parse_args()

    root = project_root()
    plan = read_json(resolve_project_path(root, args.plan_json_path, must_exist=True))
    sources = plan.get("Rows", [])
    if not isinstance(sources, list):
        sources = []
    rows = [build_row(root, source) for source in sources if isinstance(source, dict)]
    payload = {
        "Status": "pending",
        "SourcePlanPath": args.plan_json_path,
        "GeneratedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Rows": [asdict(row) for row in rows],
    }
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    write_markdown(root, report_path, rows)
    print(f"Player-operated game test result template written to: {json_path}")
    print(f"Player-operated game test result template report written to: {report_path}")
    print(f"Rows: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
