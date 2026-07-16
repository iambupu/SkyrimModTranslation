"""Run SSEDump only with explicit project-local plugin and Data paths."""

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path

from project_paths import assert_no_risky_marker, is_under, project_root, relative_path, resolve_project_path, risky_marker
from report_utils import write_text_lines as write_report


def get_plugin_masters(path: Path) -> list[str]:
    data = path.read_bytes()
    text = data.decode("ascii", errors="ignore")
    masters: list[str] = []
    pattern = re.compile(r"[A-Za-z0-9 _.'+-]+\.(?:esm|esp|esl)", re.IGNORECASE)
    for match in pattern.finditer(text):
        value = match.group(0).strip("\x00\t\r\n ")
        if value.lower() == path.name.lower():
            continue
        if value.lower().endswith((".esp", ".esm", ".esl")) and value not in masters:
            masters.append(value)
    return masters



def main() -> int:
    parser = argparse.ArgumentParser(description="Run SSEDump only against project-local plugin/data paths with safety checks.")
    parser.add_argument("--plugin-path", required=True)
    parser.add_argument("--data-path", default="")
    parser.add_argument("--ssedump-path", default="tools/SSEEdit 4.1.5f/Optional/SSEDump.exe")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--allow-missing-masters", action="store_true")
    args = parser.parse_args()

    root = project_root()
    plugin = resolve_project_path(root, args.plugin_path, must_exist=True)
    ssedump = resolve_project_path(root, args.ssedump_path, must_exist=True)
    if not plugin.is_file():
        raise ValueError(f"PluginPath must be a file: {args.plugin_path}")
    if not ssedump.is_file():
        raise ValueError(f"SSEDump executable not found: {args.ssedump_path}")

    data_path = resolve_project_path(root, args.data_path or str(plugin.parent), must_exist=True)
    if not data_path.is_dir():
        raise ValueError(f"DataPath must be a project-local directory: {args.data_path}")

    plugin_name = plugin.stem
    output = resolve_project_path(root, args.output_path or f"source/plugin_dumps/{plugin_name}.ssedump.txt", must_exist=False)
    report = resolve_project_path(root, args.report_path or f"qa/{plugin_name}.ssedump_safe_report.md", must_exist=False)

    for checked in (plugin, data_path, ssedump, output, report):
        if not is_under(checked, root):
            raise ValueError(f"Refusing path outside project root: {checked}")
        assert_no_risky_marker(checked)

    masters = get_plugin_masters(plugin)
    missing_masters = [master for master in masters if not (data_path / master).is_file()]
    lines = [
        "# Safe SSEDump Report",
        "",
        f"- Plugin: {relative_path(root, plugin)}",
        f"- DataPath: {relative_path(root, data_path)}",
        f"- SSEDump: {relative_path(root, ssedump)}",
        f"- OutputPath: {relative_path(root, output)}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Masters detected: {len(masters)}",
        f"- Missing masters: {len(missing_masters)}",
        "",
        "## Masters",
        "",
    ]
    if masters:
        for master in masters:
            lines.append(f"- {master} : exists={(data_path / master).is_file()}")
    else:
        lines.append("No masters were detected by lightweight scan.")

    if missing_masters and not args.allow_missing_masters:
        if output.is_file():
            output.unlink()
        lines.extend(
            [
                "",
                "## Result",
                "",
                "Blocked before launching SSEDump because required masters are not present in the project-local DataPath.",
                "No dump output was written; any stale output at the requested path was removed.",
                "",
                "## Safety",
                "",
                "- SSEDump was not launched.",
                "- No real game installation, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.",
                "- Copy required masters into the project sandbox only if permitted; do not point this wrapper at a real game Data directory.",
            ]
        )
        write_report(report, lines)
        print("Safe SSEDump blocked before launch: missing project-local masters.")
        print(f"Report: {report}")
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ssedump), "-q", f"-d:{data_path}", str(plugin)]
    result = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    dump_text = (result.stdout or "") + (result.stderr or "")
    marker = risky_marker(dump_text)
    if marker:
        lines.extend(["", "## Result", "", f"Blocked: SSEDump output contained forbidden external path marker '{marker}'."])
        write_report(report, lines)
        raise ValueError(f"SSEDump output contained a forbidden external path marker. See {report}")

    output.write_text(dump_text, encoding="utf-8")
    tool_error_detected = bool(re.search(r"Unexpected Error|System Error\.\s+Code:\s+2", dump_text))
    tool_missing_masters: list[str] = []
    for match in re.finditer(r'Adding master "([^"]+)"', dump_text):
        master = match.group(1)
        if not (data_path / master).is_file() and master not in tool_missing_masters:
            tool_missing_masters.append(master)

    lines.extend(
        [
            "",
            "## Result",
            "",
            "- SSEDump launched: true",
            f"- ExitCode: {result.returncode}",
            f"- Output bytes: {output.stat().st_size}",
            f"- Tool error detected: {tool_error_detected}",
            f"- Tool missing masters: {len(tool_missing_masters)}",
        ]
    )
    for master in tool_missing_masters:
        lines.append(f"  - {master}")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- SSEDump was launched only with project-local plugin and DataPath.",
            "- Output was scanned for forbidden external path markers.",
            "- No writeback, import, save, install, or binary modification was performed.",
        ]
    )
    write_report(report, lines)
    print(f"Safe SSEDump report: {report}")
    print(f"Safe SSEDump output: {output}")
    if tool_error_detected or tool_missing_masters:
        print("Safe SSEDump blocked: tool reported an error or missing project-local masters.")
        return 3
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
