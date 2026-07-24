"""Verify the user-managed or leased shared .NET SDK used by adapters.

The script only checks the configured executable and writes a QA report. It does
not download or install SDKs.
"""

import argparse
import subprocess
from datetime import datetime

from managed_tool_resolver import leased_payload_path, load_workspace_tool_config
from project_paths import project_root, resolve_project_path
from report_utils import write_text_lines as write_report



def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the controlled .NET SDK used by adapters.")
    parser.add_argument("--dotnet-path", default="")
    parser.add_argument("--report-output-path", default="qa/dotnet_sdk_check.md")
    args = parser.parse_args()

    root = project_root()
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    config = load_workspace_tool_config(root)
    if args.dotnet_path:
        decoder = config.setdefault("DecoderTools", {})
        if not isinstance(decoder, dict):
            raise ValueError("DecoderTools must be an object")
        decoder["DotNetSdkPath"] = args.dotnet_path
    try:
        with leased_payload_path(
            root,
            config,
            "DotNetSdkPath",
            command="check controlled dotnet SDK",
        ) as resolution:
            if resolution.path is None:
                raise FileNotFoundError("controlled .NET SDK is unavailable")
            dotnet = resolution.path
            lines = [
                "# Project .NET SDK Check",
                "",
                f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"- DotNetPath: {dotnet}",
                f"- Provenance: {resolution.provenance.value}",
                f"- Exists: {dotnet.is_file()}",
                "",
                "## Result",
                "",
            ]
            result = subprocess.run(
                [str(dotnet), "--info"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        lines = [
            "# Project .NET SDK Check",
            "",
            f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "- Exists: False",
            "",
            "## Result",
            "",
            f"Controlled .NET SDK is unavailable: {exc}",
            "",
            "Run auto tool setup to publish/rebind the pinned shared SDK, or "
            "configure an explicit validated external DotNetSdkPath.",
        ]
        write_report(report, lines)
        print(f"Controlled .NET SDK unavailable: {exc}")
        print(f"Report: {report}")
        return 1
    lines.extend(["```text", (result.stdout or result.stderr or "").strip(), "```", "", "## Safety", "", "- This script did not download or install anything.", "- This script did not access real game installations, MO2/Vortex, Steam, or Documents/My Games paths.", "- A managed SDK, when selected, was read from the versioned Local AppData cache under a runtime lease."])
    write_report(report, lines)
    print(f"Controlled .NET SDK checked: {dotnet}")
    print(f"Report: {report}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
