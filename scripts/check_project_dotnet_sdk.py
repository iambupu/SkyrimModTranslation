"""Verify the project-local .NET SDK used by Mutagen adapters.

The script only checks the configured executable and writes a QA report. It does
not download or install SDKs.
"""

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from project_paths import project_root, relative_path, resolve_project_path


def write_report(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the project-local .NET SDK used by Mutagen adapters.")
    parser.add_argument("--dotnet-path", default="tools/dotnet-sdk/dotnet.exe")
    parser.add_argument("--report-output-path", default="qa/dotnet_sdk_check.md")
    args = parser.parse_args()

    root = project_root()
    dotnet = resolve_project_path(root, args.dotnet_path, must_exist=False)
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    lines = [
        "# Project .NET SDK Check",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- DotNetPath: {relative_path(root, dotnet)}",
        f"- Exists: {dotnet.is_file()}",
        "",
        "## Result",
        "",
    ]
    if not dotnet.is_file():
        lines.extend(
            [
                "Missing project-local dotnet.exe.",
                "",
                "Install or copy a .NET SDK into `tools/dotnet-sdk/`, or update `config/tools.local.json` `DecoderTools.DotNetSdkPath` to a project-approved local SDK path.",
                "",
                "This script does not download installers and does not invoke installer wrapper scripts.",
            ]
        )
        write_report(report, lines)
        print(f"Project-local .NET SDK missing: {dotnet}")
        print(f"Report: {report}")
        return 1

    result = subprocess.run(
        [str(dotnet), "--info"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines.extend(["```text", (result.stdout or result.stderr or "").strip(), "```", "", "## Safety", "", "- This script did not download or install anything.", "- This script did not access real Skyrim, MO2/Vortex, Steam, AppData, or Documents/My Games paths."])
    write_report(report, lines)
    print(f"Project-local .NET SDK exists: {dotnet}")
    print(f"Report: {report}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
