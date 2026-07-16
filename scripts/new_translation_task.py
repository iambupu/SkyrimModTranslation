"""Create normalized task/report scaffolding for a new translation input."""

import argparse
import json

from route_translation_task import route_for, route_payload
from project_paths import project_root, safe_file_name
from project_paths import resolve_project_path, relative_path
from report_utils import write_text_lines as write_text









def main() -> int:
    parser = argparse.ArgumentParser(description="Create a project-local translation task folder from router output.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--task-type", default="")
    args = parser.parse_args()

    root = project_root()
    source = resolve_project_path(root, args.source_file, must_exist=True)
    route = route_for(root, source)

    safe_mod_name = safe_file_name(args.mod_name)
    if not safe_mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")
    task_dir = resolve_project_path(root, f"work/tasks/{safe_mod_name}", must_exist=False)
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    relative_source = relative_path(root, source)

    write_text(
        task_dir / "task.md",
        [
            "# Translation Task",
            "",
            f"- ModName: {args.mod_name}",
            f"- Source file: {relative_source}",
            f"- TaskType: {args.task_type}",
            f"- Recommended Skill: {route.skill}",
            f"- Primary Tool: {route.primary_tool}",
            f"- Auxiliary Tool: {route.auxiliary_tool}",
            f"- Risk: {route.risk}",
            f"- Agent Allowed: {route.agent_allowed}",
            "",
            "## Next Steps",
            "",
            route.notes,
            "",
            "1. Read the recommended Skill before handling the file.",
            "2. Work only on project-local text copies or tool exports.",
            "3. Write output into this task output folder, translated/, out/, or qa/.",
            "4. Run QA validation after edits.",
        ],
    )
    write_text(task_dir / "source_file.txt", [str(source)])
    write_text(
        task_dir / "routing.md",
        [
            "# Routing",
            "",
            f"- Recommended Skill: {route.skill}",
            f"- Primary Tool: {route.primary_tool}",
            f"- Auxiliary Tool: {route.auxiliary_tool}",
            f"- Risk: {route.risk}",
            f"- Notes: {route.notes}",
        ],
    )
    write_text(task_dir / "routing.json", [json.dumps(route_payload(route), ensure_ascii=False, indent=2)])
    write_text(task_dir / "glossary.md", ["# Task Glossary", "", "TBD."])
    write_text(task_dir / "qa.md", ["# Task QA", "", "TBD."])

    print(f"Translation task created: {task_dir}")
    print(f"Recommended Skill: {route.skill}")
    print(f"Primary Tool: {route.primary_tool}")
    print(f"Risk: {route.risk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
