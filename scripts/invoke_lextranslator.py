"""Validate and launch LexTranslator through the configured project policy.

This wrapper logs intent and path safety; GUI automation or save evidence must
still come from the GUI adapter and QA gates.
"""

import argparse
import subprocess

from project_paths import append_tool_log, bool_config, configured_path, project_root, read_json, resolve_project_path


def launch_tool(tool_name: str, config_key: str, input_path: str, mode: str) -> int:
    root = project_root()
    resolved_input = resolve_project_path(root, input_path, must_exist=True)
    config_path = resolve_project_path(root, "config/tools.local.json", must_exist=False)
    if not config_path.is_file():
        message = f"Missing config/tools.local.json. Copy config/tools.example.json and fill {config_key}."
        append_tool_log(root, tool=tool_name, input_path=resolved_input, mode=mode, status="blocked", next_action=message)
        print(message)
        print("Template: use config/tools.example.json from the installed plugin source.")
        return 1

    config = read_json(config_path)
    if not bool_config(config, "AllowLaunchGuiTools", False):
        message = f"AllowLaunchGuiTools is false. {tool_name} GUI automation is blocked for input: {resolved_input}"
        append_tool_log(root, tool=tool_name, input_path=resolved_input, mode=mode, status="blocked", next_action=message)
        print(message)
        return 0

    tool_path = configured_path(root, config.get(config_key, ""))
    if tool_path is None or not tool_path.is_file():
        message = f"{config_key} is missing or does not exist in config/tools.local.json."
        append_tool_log(root, tool=tool_name, input_path=resolved_input, mode=mode, status="blocked", next_action=message)
        print(message)
        return 1

    subprocess.Popen([str(tool_path)], cwd=str(tool_path.parent))
    next_action = (
        f"{tool_name} launched for GUI automation. Continue only with project-local input: {resolved_input}, "
        "and save outputs under translated/tool_outputs/<ModName>/ or out/<ModName>/tool_outputs/. Do not write outside the project."
    )
    append_tool_log(root, tool=tool_name, input_path=resolved_input, mode=mode, status="launched", next_action=next_action)
    print(next_action)
    return 0


def launcher_main(tool_name: str, config_key: str) -> int:
    parser = argparse.ArgumentParser(description=f"Launch {tool_name} with project-local input logging.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--optional-mode", default="manual-open")
    args = parser.parse_args()
    return launch_tool(tool_name, config_key, args.input_path, args.optional_mode)


def main() -> int:
    return launcher_main("LexTranslator", "LexTranslatorPath")


if __name__ == "__main__":
    raise SystemExit(main())
