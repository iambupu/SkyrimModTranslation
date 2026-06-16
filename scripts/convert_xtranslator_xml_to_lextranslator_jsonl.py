import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    return "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip()


def node_text(node: ElementTree.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert xTranslator XML strings to LexTranslator JSONL dictionary rows.")
    parser.add_argument("--input-xml-path", required=True)
    parser.add_argument("--output-jsonl-path", default="")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--from-language", type=int, default=1)
    parser.add_argument("--to-language", type=int, default=51)
    args = parser.parse_args()

    root = project_root()
    input_path = resolve_project_path(root, args.input_xml_path, must_exist=True)
    xml_root = ElementTree.fromstring(input_path.read_text(encoding="utf-8-sig"))

    mod_name = args.mod_name.strip()
    if not mod_name:
        addon = xml_root.find(".//Params/Addon")
        mod_name = node_text(addon).strip() or input_path.stem

    safe_mod_name = safe_file_name(Path(mod_name).stem)
    if not safe_mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")
    output_value = args.output_jsonl_path or f"translated/lextranslator_ready/{safe_mod_name}/{input_path.stem}.lextranslator.jsonl"
    output_path = resolve_project_path(root, output_value, must_exist=False)

    rows: list[dict[str, object]] = []
    for item in xml_root.findall(".//String"):
        source = node_text(item.find("Source"))
        dest = node_text(item.find("Dest"))
        if not source.strip() or not dest.strip():
            continue
        rows.append(
            {
                "From": args.from_language,
                "To": args.to_language,
                "ExactMatch": 1,
                "IgnoreCase": 0,
                "ModName": mod_name,
                "Type": "",
                "Source": source,
                "Result": dest,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + ("\n" if rows else ""), encoding="utf-8")

    report_path = resolve_project_path(root, "qa/lextranslator_ready_report.md", must_exist=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            [
                "# LexTranslator Ready Report",
                "",
                f"- Input XML: {relative_path(root, input_path)}",
                f"- Output JSONL: {relative_path(root, output_path)}",
                f"- ModName: {mod_name}",
                f"- FromLanguage: {args.from_language}",
                f"- ToLanguage: {args.to_language}",
                f"- Rows: {len(rows)}",
                f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "## Safety",
                "",
                "- Output was written only inside the project.",
                "- This script did not modify LexTranslator installation files.",
                "- This script did not modify plugin binaries.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"LexTranslator JSONL written to: {output_path}")
    print(f"LexTranslator ready report written to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
