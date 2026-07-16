"""Small wrapper that delegates LexTranslator GUI work to the automation script."""

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from project_paths import append_tool_log, bool_config, configured_path, is_under, plugin_script_path, project_root, read_json, resolve_project_path


BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".pex", ".bsa", ".ba2", ".dll", ".exe"}


def write_blocked_report(report_path: Path, input_path: Path, mode: str, reason: str) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            [
                "# LexTranslator GUI Report",
                "",
                "- Status: blocked",
                f"- Input: {input_path}",
                f"- Mode: {mode}",
                f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"- Reason: {reason}",
                "",
                "No GUI process was launched and no binary file was modified.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the LexTranslator GUI automation adapter with project-local path checks.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--mode", choices=["inspect", "open", "open-save", "open-translate-save", "save-current"], default="open")
    parser.add_argument("--translation-pairs-path", default="")
    parser.add_argument("--report-output-path", default="qa/lextranslator_gui_report.md")
    parser.add_argument("--timeout-seconds", type=int, default=45)
    args = parser.parse_args()

    root = project_root()
    input_path = resolve_project_path(root, args.input_path, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    config_path = resolve_project_path(root, "config/tools.local.json", must_exist=False)
    if not config_path.is_file():
        reason = "Missing config/tools.local.json; configure the workspace before GUI fallback."
        append_tool_log(root, tool="LexTranslator", input_path=input_path, mode=args.mode, status="blocked", next_action=reason)
        write_blocked_report(report_path, input_path, args.mode, reason)
        print(reason)
        return 2
    config = read_json(config_path)
    if not bool_config(config, "AllowLaunchGuiTools", False):
        reason = "AllowLaunchGuiTools is false. LexTranslator GUI automation is blocked."
        append_tool_log(root, tool="LexTranslator", input_path=input_path, mode=args.mode, status="blocked", next_action=reason)
        write_blocked_report(report_path, input_path, args.mode, reason)
        print(reason)
        return 2
    tool_path = configured_path(root, config.get("LexTranslatorPath", ""))
    if tool_path is None or not tool_path.is_file():
        raise ValueError("LexTranslatorPath is missing or does not exist in config/tools.local.json.")

    mod_root = resolve_project_path(root, "mod", must_exist=True)
    if input_path.suffix.lower() in BINARY_EXTENSIONS and is_under(input_path, mod_root):
        raise ValueError(f"Refusing to open a binary directly from mod/: {input_path}. Copy it to out/<ModName>/tool_outputs/ first.")

    python_command = str(config.get("GuiAutomationPython", "")).strip() or "python"
    automation_script = plugin_script_path("automate-lextranslator-gui.py")
    if not automation_script.is_file():
        raise FileNotFoundError("missing plugin script: scripts/automate-lextranslator-gui.py")
    command = [
        python_command,
        str(automation_script),
        "--project-root",
        str(root),
        "--tool-path",
        str(tool_path),
        "--input-path",
        str(input_path),
        "--mode",
        args.mode,
        "--report-path",
        str(report_path),
        "--timeout",
        str(args.timeout_seconds),
    ]
    if args.translation_pairs_path.strip():
        pairs = resolve_project_path(root, args.translation_pairs_path, must_exist=True)
        command.extend(["--translation-pairs-path", str(pairs)])
    result = subprocess.run(command, cwd=str(root), check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
