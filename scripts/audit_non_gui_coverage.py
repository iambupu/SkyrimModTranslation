"""Compare extracted non-GUI candidates against delivered final_mod evidence.

Missing and unverified rows tell the strict gate whether every automatically
discoverable candidate has a corresponding final output signal.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def ensure_inside(child: Path, parent: Path) -> None:
    child_resolved = child.resolve()
    parent_resolved = parent.resolve()
    if child_resolved != parent_resolved and parent_resolved not in child_resolved.parents:
        raise SystemExit(f"unsafe path outside project: {child_resolved}")


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return cleaned.strip()


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        return Path(parent_resolved) == Path(child_resolved) or Path(parent_resolved) in Path(child_resolved).parents
    except RuntimeError:
        return False


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def read_jsonl_rows_lenient(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def fresh_pex_export_paths(project_root: Path, mod_name: str, script_stem: str, final_pex: Path) -> list[Path]:
    export_root = project_root / "source" / "pex_exports" / mod_name
    candidates = [
        export_root / f"{script_stem}.final_binary_review.pex_strings.jsonl",
        export_root / f"{script_stem}.gate_final_mod.pex_strings.jsonl",
        export_root / f"{script_stem}.strict_final_mod.pex_strings.jsonl",
    ]
    if not final_pex.is_file():
        return [path for path in candidates if path.is_file()]
    final_mtime = final_pex.stat().st_mtime
    return [path for path in candidates if path.is_file() and path.stat().st_mtime + 1 >= final_mtime]


def exact_source_in_final_pex_export(project_root: Path, mod_name: str, script_stem: str, final_pex: Path, source: str) -> bool | None:
    paths = fresh_pex_export_paths(project_root, mod_name, script_stem, final_pex)
    saw_export_rows = False
    for path in paths:
        rows = read_jsonl_rows_lenient(path)
        if rows:
            saw_export_rows = True
        for row in rows:
            if str(row.get("Source", "")) == source or str(row.get("source", "")) == source:
                return True
    if saw_export_rows:
        return False
    return None


def contains_cjk(value: str) -> bool:
    return re.search(r"[\u4e00-\u9fff]", value) is not None


def workspace_relative(candidate_file: str) -> str:
    marker = "work/extracted_mods/"
    normalized = candidate_file.replace("\\", "/")
    if marker not in normalized:
        return normalized
    remainder = normalized.split(marker, 1)[1]
    parts = remainder.split("/")
    if len(parts) <= 1:
        return ""
    data_roots = {
        "calientetools",
        "fomod",
        "interface",
        "mcm",
        "meshes",
        "scripts",
        "seq",
        "skse",
        "slanims",
        "sound",
        "textures",
        "video",
    }
    second = parts[1].lower()
    if second in data_roots or Path(parts[1]).suffix:
        return "/".join(parts[1:])
    if len(parts) >= 3:
        return "/".join(parts[2:])
    return "/".join(parts[1:])


def parse_translation_file(path: Path) -> dict[str, str]:
    result = {}
    if not path.exists():
        return result
    for line in read_text(path).splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        result[key] = value
    return result


def audit_interface(row: dict, project_root: Path, final_mod_dir: Path) -> tuple[str, str, str]:
    relative = workspace_relative(row.get("file", ""))
    source_path = Path(relative)
    final_path = final_mod_dir / source_path
    key = row.get("key", "")
    if not final_path.exists():
        return "missing", "direct-replacement-interface-file-missing", rel(project_root, final_path)
    translations = parse_translation_file(final_path)
    target = translations.get(key, "")
    if target and target != row.get("source", "") and contains_cjk(target):
        return "covered", "direct-replacement-interface-key-translated", rel(project_root, final_path)
    if target:
        return "unverified", "direct-replacement-key-without-cjk-or-same-source", rel(project_root, final_path)
    return "missing", "direct-replacement-interface-key-missing", rel(project_root, final_path)


def audit_text_asset(row: dict, project_root: Path, final_mod_dir: Path) -> tuple[str, str, str]:
    relative = workspace_relative(row.get("file", ""))
    final_path = final_mod_dir / relative
    if not final_path.exists():
        return "missing", "final-text-file-missing", rel(project_root, final_path)
    text = read_text(final_path)
    source = row.get("source", "")
    if source and source in text:
        return "missing", "source-still-present-in-final-text", rel(project_root, final_path)
    if contains_cjk(text):
        return "covered", "source-not-present-and-cjk-present", rel(project_root, final_path)
    return "unverified", "source-not-present-but-no-cjk-found", rel(project_root, final_path)


def audit_psc(row: dict, project_root: Path, final_mod_dir: Path) -> tuple[str, str, str]:
    relative = workspace_relative(row.get("file", ""))
    source_path = Path(relative)
    script_name = source_path.with_suffix(".pex").name
    script_stem = source_path.stem
    pex_path = final_mod_dir / "Scripts" / script_name
    if not pex_path.exists():
        return "covered", "psc-source-only-no-final-pex", rel(project_root, pex_path)
    source = row.get("source", "")
    if not source:
        return "unverified", "empty-source", rel(project_root, pex_path)
    mod_name = ""
    if final_mod_dir.name.lower() == "final_mod" and len(final_mod_dir.parents) >= 2:
        mod_name = final_mod_dir.parents[1].name
    translation_path = project_root / "work" / "normalized" / mod_name / "pex_apply" / f"{script_stem}.translation.jsonl"
    verification_path = project_root / "qa" / f"{mod_name}.{script_stem}.pex_output_verification.md"
    if translation_path.is_file() and verification_path.is_file():
        for line in translation_path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("Source") == source or item.get("source") == source:
                report_text = verification_path.read_text(encoding="utf-8", errors="replace")
                if "No blocking issues." in report_text:
                    return "covered", "verified-pex-tool-output", rel(project_root, pex_path)
    data = pex_path.read_bytes()
    source_bytes = source.encode("utf-8", errors="ignore")
    if source_bytes and source_bytes in data:
        export_status = exact_source_in_final_pex_export(project_root, mod_name, script_stem, pex_path, source)
        if export_status is False:
            return "covered", "source-bytes-only-substring-no-current-final-pex-row", rel(project_root, pex_path)
        if export_status is True:
            return "missing", "source-still-present-as-current-final-pex-row", rel(project_root, pex_path)
        return "unverified", "source-bytes-substring-without-current-final-pex-export", rel(project_root, pex_path)
    return "covered", "source-not-present-in-final-pex", rel(project_root, pex_path)


def audit_row(row: dict, project_root: Path, final_mod_dir: Path) -> dict:
    kind = row.get("kind", "")
    if kind == "interface-translation":
        status, reason, checked_path = audit_interface(row, project_root, final_mod_dir)
    elif kind in {"json-string", "xml-text", "xml-attribute"}:
        status, reason, checked_path = audit_text_asset(row, project_root, final_mod_dir)
    elif kind == "psc-string-literal":
        status, reason, checked_path = audit_psc(row, project_root, final_mod_dir)
    else:
        status, reason, checked_path = "unverified", "unsupported-kind", ""
    result = dict(row)
    result["coverage_status"] = status
    result["coverage_reason"] = reason
    result["checked_path"] = checked_path
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--candidates-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    mod_name = safe_file_name(args.mod_name.strip())
    if not mod_name and args.final_mod_dir:
        final_hint = Path(args.final_mod_dir)
        mod_name = safe_file_name(final_hint.parent.name if final_hint.name.lower() == "final_mod" else final_hint.name)
    if not mod_name:
        raise SystemExit("Pass --mod-name for coverage audit.")

    candidates_path = Path(args.candidates_path) if args.candidates_path else root / "out" / mod_name / "non_gui_exports" / "translation_candidates.jsonl"
    final_mod_dir = Path(args.final_mod_dir) if args.final_mod_dir else default_final_mod_dir(root, mod_name)
    output_dir = Path(args.output_dir) if args.output_dir else root / "out" / mod_name / "qa"
    if not candidates_path.is_absolute():
        candidates_path = root / candidates_path
    if not final_mod_dir.is_absolute():
        final_mod_dir = root / final_mod_dir
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    candidates_path = candidates_path.resolve()
    final_mod_dir = final_mod_dir.resolve()
    output_dir = output_dir.resolve()
    ensure_inside(candidates_path, root)
    ensure_inside(final_mod_dir, root)
    ensure_inside(output_dir, root)
    out_root = root / "out"
    if not is_under(candidates_path, out_root):
        raise SystemExit(f"CandidatesPath must be under out/: {candidates_path}")
    if not is_under(final_mod_dir, out_root):
        raise SystemExit(f"FinalModDir must be under out/: {final_mod_dir}")
    if not is_under(output_dir, out_root):
        raise SystemExit(f"OutputDir must be under out/: {output_dir}")
    if not candidates_path.is_file():
        raise SystemExit(f"CandidatesPath does not exist: {candidates_path}")
    if not final_mod_dir.is_dir():
        raise SystemExit(f"FinalModDir does not exist: {final_mod_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_jsonl(candidates_path)
    audited = [audit_row(row, root, final_mod_dir) for row in candidates]
    covered = [row for row in audited if row["coverage_status"] == "covered"]
    missing = [row for row in audited if row["coverage_status"] == "missing"]
    unverified = [row for row in audited if row["coverage_status"] == "unverified"]

    write_jsonl(output_dir / "non_gui_coverage_all.jsonl", audited)
    write_jsonl(output_dir / "non_gui_remaining_gaps.jsonl", missing)
    write_jsonl(output_dir / "non_gui_covered_candidates.jsonl", covered)
    write_jsonl(output_dir / "non_gui_unverified_candidates.jsonl", unverified)

    by_kind: dict[str, dict[str, int]] = {}
    for row in audited:
        kind = row.get("kind", "unknown")
        status = row["coverage_status"]
        by_kind.setdefault(kind, {}).setdefault(status, 0)
        by_kind[kind][status] += 1

    report = [
        "# Non-GUI Translation Coverage Audit",
        "",
        f"- ModName: {mod_name}",
        f"- Candidates: {rel(root, candidates_path)}",
        f"- FinalModDir: {rel(root, final_mod_dir)}",
        f"- Audited candidates: {len(audited)}",
        f"- Covered: {len(covered)}",
        f"- Missing: {len(missing)}",
        f"- Unverified: {len(unverified)}",
        "",
        "## Coverage By Kind",
        "",
        "| Kind | Covered | Missing | Unverified |",
        "|---|---:|---:|---:|",
    ]
    for kind in sorted(by_kind):
        counts = by_kind[kind]
        report.append(
            f"| {kind} | {counts.get('covered', 0)} | {counts.get('missing', 0)} | {counts.get('unverified', 0)} |"
        )
    report.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Covered means the final_mod text target has a Chinese replacement, or the source bytes are no longer present in the project-local PEX copy.",
            "- Missing means the English source is still present in the final_mod text target or PEX copy.",
            "- Unverified means the audit could not prove either state.",
            "- Interface/translations coverage expects direct replacement of the original translation file name, not a *_chinese.txt sidecar.",
            "",
            "## Safety",
            "",
            "- This audit is read-only.",
            "- It does not modify ESP, PEX, PSC, or final_mod files.",
            "- PEX coverage prefers current PEX export identity; raw byte presence is only a fallback signal and is not proof of correct Papyrus behavior.",
        ]
    )
    report_path = output_dir / "non_gui_translation_coverage.md"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Coverage report: {report_path}")
    print(f"Covered: {len(covered)}")
    print(f"Missing: {len(missing)}")
    print(f"Unverified: {len(unverified)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
