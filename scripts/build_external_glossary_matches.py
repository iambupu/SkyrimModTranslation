"""Build per-Mod glossary matches from LexTranslator-style dictionaries.

LexTranslator-style dynamic dictionaries remain reference sources. This script
extracts only terms that appear in current translation inputs, producing a
compact packet for Codex/model translation without treating every entry as
approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SOURCE_FIELDS = ("source", "Source", "original", "Original", "text", "Text")
TARGET_FIELDS = ("target", "Target", "translation", "Translation", "Result", "result")
DEFAULT_EXTERNAL_GLOSSARIES = ("glossary/lextranslator_dynamic_dictionaries",)
DEFAULT_INDEX_PATH = "work/glossary_rag/lextranslator_dynamic.sqlite"
INDEX_VERSION = 1
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "your",
}
PRIVATE_USE_TRANSLATION = str.maketrans(
    {
        "\ue000": "'",
        "\ue001": '"',
        "\ue003": "-",
        "\ue004": "#",
        "\ue005": "-",
        "\ue009": "=",
        "\ue00a": "<",
        "\ue00b": ">",
        "\ue00c": "!",
        "\ue00e": "&",
        "\ue00f": "(",
        "\ue010": ")",
    }
)


@dataclass(frozen=True)
class GlossaryEntry:
    source: str
    target: str
    normalized_source: str
    glossary_path: str


@dataclass
class TextUnit:
    file: str
    line: int
    text: str
    field: str


@dataclass
class MatchRow:
    Source: str
    Target: str
    NormalizedSource: str
    Count: int
    GlossaryPath: str
    Examples: list[dict[str, object]]


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


def relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    return "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    text = value.translate(PRIVATE_USE_TRANSLATION)
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def parse_pipe_dictionary(root: Path, path: Path) -> list[GlossaryEntry]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    parts = re.split(r",(?=\d+\|\d+\|\d+\|\d+\|)", text)
    entries: list[GlossaryEntry] = []
    seen: set[tuple[str, str]] = set()
    for part in parts:
        fields = part.strip().split("|")
        if len(fields) < 8:
            continue
        source = fields[-2].strip()
        target = fields[-1].strip()
        if not source or not target:
            continue
        normalized = normalize_text(source)
        if len(normalized) < 3:
            continue
        key = (normalized, target)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            GlossaryEntry(
                source=source,
                target=target,
                normalized_source=normalized,
                glossary_path=relative_path(root, path),
            )
        )
    return entries


def expand_glossary_files(root: Path, glossary_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in glossary_paths:
        path = resolve_project_path(root, value, must_exist=True)
        if not is_under(path, root / "glossary"):
            raise ValueError(f"Glossary path must be under glossary/: {value}")
        if path.is_dir():
            files.extend(sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in {".txt", ".csv", ".dict"}))
        else:
            files.append(path)
    unique: dict[str, Path] = {}
    for path in files:
        unique[str(path.resolve(strict=True)).lower()] = path
    return sorted(unique.values(), key=lambda item: relative_path(root, item).lower())


def glossary_fingerprint(root: Path, glossary_paths: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in expand_glossary_files(root, glossary_paths):
        stat = path.stat()
        rows.append(
            {
                "path": relative_path(root, path),
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    rows.sort(key=lambda item: str(item["path"]).lower())
    return rows


def latest_glossary_mtime(root: Path, glossary_paths: list[str]) -> float:
    latest = 0.0
    for value in glossary_paths:
        path = resolve_project_path(root, value, must_exist=True)
        if not is_under(path, root / "glossary"):
            raise ValueError(f"Glossary path must be under glossary/: {value}")
        latest = max(latest, path.stat().st_mtime)
    files = expand_glossary_files(root, glossary_paths)
    for path in files:
        latest = max(latest, path.stat().st_mtime)
    return latest


def read_index_metadata(index_path: Path) -> dict[str, str]:
    if not index_path.is_file():
        return {}
    try:
        with sqlite3.connect(index_path) as conn:
            return {str(key): str(value) for key, value in conn.execute("SELECT key, value FROM metadata")}
    except sqlite3.DatabaseError:
        return {}


def index_is_current(index_path: Path, fingerprint: list[dict[str, object]]) -> bool:
    metadata = read_index_metadata(index_path)
    if metadata.get("index_version") != str(INDEX_VERSION):
        return False
    return metadata.get("glossary_fingerprint") == json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)


def rebuild_index(root: Path, index_path: Path, glossary_paths: list[str], fingerprint: list[dict[str, object]]) -> int:
    entries = load_glossary_entries(root, glossary_paths)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index_path.unlink()
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE entries(
              id INTEGER PRIMARY KEY,
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              normalized_source TEXT NOT NULL,
              glossary_path TEXT NOT NULL,
              source_length INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(normalized_source, content='entries', content_rowid='id')")
        conn.executemany(
            """
            INSERT INTO entries(source, target, normalized_source, glossary_path, source_length)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (entry.source, entry.target, entry.normalized_source, entry.glossary_path, len(entry.normalized_source))
                for entry in entries
            ],
        )
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [
                ("index_version", str(INDEX_VERSION)),
                ("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ("entry_count", str(len(entries))),
                ("glossary_fingerprint", json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)),
            ],
        )
        conn.commit()
    return len(entries)


def ensure_index(root: Path, index_path: Path, glossary_paths: list[str], force_rebuild: bool = False) -> int:
    glossary_mtime = latest_glossary_mtime(root, glossary_paths)
    # Fast path for normal runs: mtime avoids hashing the large dictionaries when nothing changed.
    if not force_rebuild and index_path.is_file() and index_path.stat().st_mtime >= glossary_mtime:
        metadata = read_index_metadata(index_path)
        if metadata.get("index_version") == str(INDEX_VERSION):
            return int(metadata.get("entry_count", "0") or "0")
    # Fingerprint fallback catches same-second timestamp quirks or copied files with preserved mtimes.
    fingerprint = glossary_fingerprint(root, glossary_paths)
    if not force_rebuild and index_is_current(index_path, fingerprint):
        metadata = read_index_metadata(index_path)
        return int(metadata.get("entry_count", "0") or "0")
    return rebuild_index(root, index_path, glossary_paths, fingerprint)


def json_value(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, str]:
    for field in fields:
        value = row.get(field)
        if value is not None:
            return field, str(value)
    return "", ""


def read_jsonl_units(root: Path, path: Path) -> list[TextUnit]:
    units: list[TextUnit] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        field, source = json_value(row, SOURCE_FIELDS)
        if source.strip():
            units.append(TextUnit(relative_path(root, path), line_number, source, field or "source"))
    return units


def read_json_units(root: Path, path: Path) -> list[TextUnit]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return []
    units: list[TextUnit] = []
    if isinstance(payload, dict):
        for index, (key, value) in enumerate(payload.items(), start=1):
            if isinstance(value, str) and value.strip() and key in SOURCE_FIELDS:
                units.append(TextUnit(relative_path(root, path), index, value, key))
            elif isinstance(key, str) and key.strip():
                # Translation-map templates use source text as JSON object keys.
                units.append(TextUnit(relative_path(root, path), index, key, "json_key"))
    elif isinstance(payload, list):
        for index, row in enumerate(payload, start=1):
            if not isinstance(row, dict):
                continue
            field, source = json_value(row, SOURCE_FIELDS)
            if source.strip():
                units.append(TextUnit(relative_path(root, path), index, source, field or "source"))
    return units


def read_text_units(root: Path, path: Path) -> list[TextUnit]:
    units: list[TextUnit] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if stripped:
            units.append(TextUnit(relative_path(root, path), line_number, stripped, "line"))
    return units


def iter_input_files(root: Path, input_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in input_paths:
        path = resolve_project_path(root, value, must_exist=True)
        if path.is_dir():
            files.extend(
                sorted(
                    item
                    for item in path.rglob("*")
                    if item.is_file() and item.suffix.lower() in {".jsonl", ".json", ".txt", ".csv", ".xml"}
                )
            )
        else:
            files.append(path)
    return files


def collect_text_units(root: Path, files: list[Path]) -> list[TextUnit]:
    units: list[TextUnit] = []
    for path in files:
        suffix = path.suffix.lower()
        try:
            if suffix == ".jsonl":
                units.extend(read_jsonl_units(root, path))
            elif suffix == ".json":
                units.extend(read_json_units(root, path))
            elif suffix in {".txt", ".csv", ".xml"}:
                units.extend(read_text_units(root, path))
        except UnicodeError:
            continue
    return units


def default_input_paths(root: Path, mod_name: str) -> list[str]:
    candidates = [
        root / "work" / "normalized" / mod_name,
        root / "work" / "plugin_translation_maps" / mod_name,
        root / "source" / "plugin_exports" / mod_name,
        root / "translated" / "plugin_exports" / mod_name,
        root / "translated" / "final_mod" / mod_name,
    ]
    return [relative_path(root, path) for path in candidates if path.exists()]


def load_glossary_entries(root: Path, glossary_paths: list[str]) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    for path in expand_glossary_files(root, glossary_paths):
        entries.extend(parse_pipe_dictionary(root, path))
    entries.sort(key=lambda item: (-len(item.normalized_source), item.normalized_source, item.target))
    return entries


def text_contains_term(normalized_text: str, normalized_term: str) -> bool:
    if " " in normalized_term or any(not char.isalnum() for char in normalized_term):
        return normalized_term in normalized_text
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", normalized_text) is not None


def build_matches(entries: list[GlossaryEntry], units: list[TextUnit], max_examples: int, max_matches: int) -> list[MatchRow]:
    normalized_units = [(unit, normalize_text(unit.text)) for unit in units if unit.text.strip()]
    rows: list[MatchRow] = []
    for entry in entries:
        examples: list[dict[str, object]] = []
        count = 0
        for unit, normalized_text in normalized_units:
            if not text_contains_term(normalized_text, entry.normalized_source):
                continue
            count += 1
            if len(examples) < max_examples:
                examples.append({"file": unit.file, "line": unit.line, "field": unit.field, "text": unit.text})
        if count:
            rows.append(
                MatchRow(
                    Source=entry.source,
                    Target=entry.target,
                    NormalizedSource=entry.normalized_source,
                    Count=count,
                    GlossaryPath=entry.glossary_path,
                    Examples=examples,
                )
            )
        if len(rows) >= max_matches:
            break
    rows.sort(key=lambda item: (-item.Count, item.NormalizedSource, item.Target))
    return rows


def fts_query_for_text(normalized_text: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9'_ -]{2,}", normalized_text)
        if token.strip("-_ '") and token not in STOPWORDS
    ]
    split_tokens: list[str] = []
    for token in tokens:
        split_tokens.extend(part for part in re.split(r"[^a-z0-9]+", token) if len(part) >= 3 and part not in STOPWORDS)
    unique = sorted(set(split_tokens), key=lambda item: (-len(item), item))[:16]
    return " OR ".join(f'"{token}"' for token in unique)


def query_index_for_unit(conn: sqlite3.Connection, normalized_text: str, candidate_limit: int) -> list[sqlite3.Row]:
    query = fts_query_for_text(normalized_text)
    if not query:
        return []
    return list(
        conn.execute(
            """
            SELECT e.id, e.source, e.target, e.normalized_source, e.glossary_path
            FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
            ORDER BY e.source_length DESC, e.normalized_source ASC
            LIMIT ?
            """,
            (query, candidate_limit),
        )
    )


def build_matches_from_index(index_path: Path, units: list[TextUnit], max_examples: int, max_matches: int, candidate_limit: int) -> list[MatchRow]:
    matches: dict[int, MatchRow] = {}
    normalized_units = [(unit, normalize_text(unit.text)) for unit in units if unit.text.strip()]
    with sqlite3.connect(index_path) as conn:
        conn.row_factory = sqlite3.Row
        for unit, normalized_text in normalized_units:
            for row in query_index_for_unit(conn, normalized_text, candidate_limit):
                normalized_source = str(row["normalized_source"])
                if not text_contains_term(normalized_text, normalized_source):
                    continue
                row_id = int(row["id"])
                existing = matches.get(row_id)
                example = {"file": unit.file, "line": unit.line, "field": unit.field, "text": unit.text}
                if existing is None:
                    matches[row_id] = MatchRow(
                        Source=str(row["source"]),
                        Target=str(row["target"]),
                        NormalizedSource=normalized_source,
                        Count=1,
                        GlossaryPath=str(row["glossary_path"]),
                        Examples=[example],
                    )
                else:
                    existing.Count += 1
                    if len(existing.Examples) < max_examples:
                        existing.Examples.append(example)
    rows = list(matches.values())
    rows.sort(key=lambda item: (-item.Count, item.NormalizedSource, item.Target))
    return rows[:max_matches]


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_outputs(root: Path, mod_name: str, rows: list[MatchRow], units: list[TextUnit], output_dir: Path, report_path: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "external_glossary_matches.jsonl"
    manifest_path = output_dir / "manifest.json"
    md_path = output_dir / "external_glossary_matches.md"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        f"# External Glossary Matches: {mod_name}",
        "",
        f"- Created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Text units scanned: {len(units)}",
        f"- Matched glossary terms: {len(rows)}",
        f"- Complete JSONL: {relative_path(root, jsonl_path)}",
        "",
        "## Use During Translation",
        "",
        "- Treat these rows as high-priority terminology hints, not automatic replacements.",
        "- Preserve protected tokens, file names, script names, placeholders, and runtime keys even if a glossary entry looks similar.",
        "- If a glossary entry conflicts with Mod context, keep context and record the conflict in qa/unresolved_terms.md.",
        "",
        "## Matches",
        "",
    ]
    if rows:
        lines.extend(["| Source | Suggested Chinese | Count | Examples |", "|---|---|---:|---|"])
        for row in rows[:300]:
            examples = "; ".join(f"{item['file']}:{item['line']}" for item in row.Examples[:3])
            lines.append(f"| {markdown_cell(row.Source)} | {markdown_cell(row.Target)} | {row.Count} | {markdown_cell(examples)} |")
        if len(rows) > 300:
            lines.extend(["", f"Preview limited to 300 rows. Use `{relative_path(root, jsonl_path)}` for all matches."])
    else:
        lines.append("No external glossary terms matched the current input set.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "ModName": mod_name,
                "CreatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "TextUnitsScanned": len(units),
                "MatchedTerms": len(rows),
                "JsonlPath": relative_path(root, jsonl_path),
                "MarkdownPath": relative_path(root, md_path),
                "ReportPath": relative_path(root, report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact per-Mod match list from external Skyrim glossaries.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--input-path", action="append", default=[])
    parser.add_argument("--external-glossary-path", action="append", default=[])
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument("--max-matches", type=int, default=2000)
    parser.add_argument("--candidate-limit-per-text", type=int, default=200)
    parser.add_argument("--index-path", default=DEFAULT_INDEX_PATH)
    parser.add_argument("--rebuild-index", action="store_true")
    args = parser.parse_args()

    root = project_root()
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty.")
    glossary_paths = args.external_glossary_path or list(DEFAULT_EXTERNAL_GLOSSARIES)
    input_paths = args.input_path or default_input_paths(root, mod_name)
    if not input_paths:
        raise ValueError(f"No translation input paths found for {mod_name}.")

    output_dir = resolve_project_path(root, args.output_dir or f"work/glossary_matches/{mod_name}", must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.external_glossary_matches.md", must_exist=False)
    index_path = resolve_project_path(root, args.index_path, must_exist=False)
    if not is_under(output_dir, root / "work"):
        raise ValueError("OutputDir must be under work/.")
    if not is_under(report_path, root / "qa"):
        raise ValueError("ReportOutputPath must be under qa/.")
    if not is_under(index_path, root / "work"):
        raise ValueError("IndexPath must be under work/.")

    entry_count = ensure_index(root, index_path, glossary_paths, args.rebuild_index)
    files = iter_input_files(root, input_paths)
    units = collect_text_units(root, files)
    rows = build_matches_from_index(index_path, units, args.max_examples, args.max_matches, args.candidate_limit_per_text)
    write_outputs(root, mod_name, rows, units, output_dir, report_path)

    print(f"External glossary entries indexed: {entry_count}")
    print(f"Glossary RAG index: {index_path}")
    print(f"Text units scanned: {len(units)}")
    print(f"Matched terms: {len(rows)}")
    print(f"Glossary match report written to: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"External glossary match build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
