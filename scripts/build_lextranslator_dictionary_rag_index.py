"""Build the local RAG index for LexTranslator-style dynamic dictionaries."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from build_external_glossary_matches import (
    DEFAULT_EXTERNAL_GLOSSARIES,
    DEFAULT_INDEX_PATH,
    ensure_index,
    expand_glossary_files,
    is_under,
    latest_glossary_mtime,
    project_root,
    read_index_metadata,
    relative_path,
    resolve_project_path,
)


def timestamp(value: float) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def write_report(
    root: Path,
    index_path: Path,
    report_path: Path,
    glossary_paths: list[str],
    entry_count: int,
    dictionary_mtime: float,
    index_mtime_before: float,
    force: bool,
) -> None:
    metadata = read_index_metadata(index_path)
    glossary_files = [relative_path(root, path) for path in expand_glossary_files(root, glossary_paths)]
    index_mtime_after = index_path.stat().st_mtime if index_path.is_file() else 0.0
    rebuilt = index_mtime_before <= 0 or index_mtime_after > index_mtime_before + 1e-6
    if force and rebuilt:
        refresh_decision = "rebuilt_forced"
    elif index_mtime_before <= 0 and rebuilt:
        refresh_decision = "rebuilt_missing_index"
    elif index_mtime_before < dictionary_mtime and rebuilt:
        refresh_decision = "rebuilt_dictionary_newer_than_index"
    elif index_mtime_before < dictionary_mtime:
        refresh_decision = "reused_index_current_by_fingerprint"
    else:
        refresh_decision = "reused_index_current_by_mtime"
    lines = [
        "# LexTranslator Dynamic Dictionary RAG Index",
        "",
        f"- Built at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Index path: {relative_path(root, index_path)}",
        f"- Indexed entries: {entry_count}",
        f"- Index version: {metadata.get('index_version', '')}",
        f"- Refresh decision: {refresh_decision}",
        f"- Dynamic dictionary latest mtime: {timestamp(dictionary_mtime)}",
        f"- Index mtime before run: {timestamp(index_mtime_before)}",
        f"- Index mtime after run: {timestamp(index_mtime_after)}",
        "",
        "## Source Dictionaries",
        "",
    ]
    for path in glossary_files:
        lines.append(f"- `{path}`")
    lines.extend(
        [
            "",
            "## Usage",
            "",
            "- The index is used by `scripts/build_external_glossary_matches.py`.",
            "- It is a retrieval aid for translation prompts, not an automatic replacement table.",
            "- Rebuild after adding or changing files under `glossary/lextranslator_dynamic_dictionaries/`.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_path = report_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "BuiltAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "IndexPath": relative_path(root, index_path),
                "IndexedEntries": entry_count,
                "IndexVersion": metadata.get("index_version", ""),
                "RefreshDecision": refresh_decision,
                "DynamicDictionaryLatestMTime": timestamp(dictionary_mtime),
                "IndexMTimeBeforeRun": timestamp(index_mtime_before),
                "IndexMTimeAfterRun": timestamp(index_mtime_after),
                "SourceDictionaryInputs": glossary_paths,
                "SourceDictionaryFiles": glossary_files,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local SQLite/FTS RAG index for LexTranslator-style dictionaries.")
    parser.add_argument("--external-glossary-path", action="append", default=[])
    parser.add_argument("--index-path", default=DEFAULT_INDEX_PATH)
    parser.add_argument("--report-output-path", default="qa/lextranslator_dictionary_rag_index.md")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    glossary_paths = args.external_glossary_path or list(DEFAULT_EXTERNAL_GLOSSARIES)
    index_path = resolve_project_path(root, args.index_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    if not is_under(index_path, root / "work"):
        raise ValueError("IndexPath must be under work/.")
    if not is_under(report_path, root / "qa"):
        raise ValueError("ReportOutputPath must be under qa/.")

    dictionary_mtime = latest_glossary_mtime(root, glossary_paths)
    index_mtime_before = index_path.stat().st_mtime if index_path.is_file() else 0.0
    entry_count = ensure_index(root, index_path, glossary_paths, force_rebuild=args.force)
    write_report(root, index_path, report_path, glossary_paths, entry_count, dictionary_mtime, index_mtime_before, args.force)
    print(f"LexTranslator dictionary RAG index: {index_path}")
    print(f"Indexed entries: {entry_count}")
    print(f"Report written to: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"LexTranslator dictionary RAG index build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
