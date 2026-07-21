"""Build and validate model-authored Mod translation context evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from game_context import GameContext, game_display_label
from report_utils import markdown_cell


SOURCE_EVIDENCE_FIELDS = ("File", "Line", "Type", "Risk", "Context", "Source")
REQUIRED_CONTEXT_TEXT_FIELDS = ("summary", "purpose", "tone", "confidence")
REQUIRED_CONTEXT_LIST_FIELDS = ("features", "evidence_files")


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _normalized(value: object) -> str:
    return " ".join(_text(value).split()).casefold()


def _group_text(value: object) -> str:
    return " ".join(_text(value).split())


def _stable_group_id(prefix: str, *parts: object) -> str:
    payload = json.dumps([_group_text(part) for part in parts], ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def source_evidence_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for row in rows:
        source = _text(row.get("Source"))
        if not source:
            continue
        evidence.append(
            {
                field: int(row.get(field, 0) or 0) if field == "Line" else _text(row.get(field))
                for field in SOURCE_EVIDENCE_FIELDS
            }
        )
    return sorted(
        evidence,
        key=lambda item: tuple(str(item.get(field, "")).casefold() for field in SOURCE_EVIDENCE_FIELDS),
    )


def source_rows_hash(rows: list[dict[str, object]]) -> str:
    payload = json.dumps(
        source_evidence_rows(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aggregate_source_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in source_evidence_rows(rows):
        source = _text(row.get("Source"))
        context_key = _text(row.get("Context"))
        key = tuple(
            _group_text(value)
            for value in (source, row.get("Type"), row.get("Risk"), context_key)
        )
        group = grouped.setdefault(
            key,
            {
                "GroupId": _stable_group_id("source", *key),
                "Source": source,
                "Type": key[1],
                "Risk": key[2],
                "ContextKey": context_key,
                "OccurrenceCount": 0,
                "Occurrences": [],
            },
        )
        group["OccurrenceCount"] = int(group["OccurrenceCount"]) + 1
        occurrences = group["Occurrences"]
        assert isinstance(occurrences, list)
        occurrences.append(
            {
                "File": row["File"],
                "Line": row["Line"],
                "Context": row["Context"],
            }
        )
    return sorted(
        grouped.values(),
        key=lambda item: (
            str(item["Source"]).casefold(),
            str(item["Type"]).casefold(),
            str(item["Risk"]).casefold(),
            str(item["ContextKey"]).casefold(),
        ),
    )


def aggregate_review_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    source_targets: dict[str, set[str]] = {}
    for row in rows:
        source = _text(row.get("Source"))
        target = _text(row.get("Target"))
        if source:
            source_targets.setdefault(_normalized(source), set()).add(_normalized(target))

    grouped: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    for row in rows:
        source = _text(row.get("Source"))
        target = _text(row.get("Target"))
        if not source and not target:
            continue
        row_type = _text(row.get("Type"))
        risk = _text(row.get("Risk"))
        context_key = _text(row.get("Context"))
        key = tuple(_group_text(value) for value in (source, target, row_type, risk, context_key))
        group = grouped.setdefault(
            key,
            {
                "GroupId": _stable_group_id("review", *key),
                "Source": source,
                "Target": target,
                "Type": row_type,
                "Risk": risk,
                "ContextKey": context_key,
                "OccurrenceCount": 0,
                "Occurrences": [],
                "SemanticFocus": [],
            },
        )
        group["OccurrenceCount"] = int(group["OccurrenceCount"]) + 1
        occurrences = group["Occurrences"]
        assert isinstance(occurrences, list)
        occurrences.append(
            {
                "File": _text(row.get("File")),
                "Line": int(row.get("Line", 0) or 0),
                "Context": _text(row.get("Context")),
            }
        )

    for group in grouped.values():
        source = str(group["Source"])
        row_type = str(group["Type"]).casefold()
        focus: list[str] = []
        if len(source.split()) <= 8 and any(
            marker in row_type for marker in ("ui", "mcm", "label", "interface", "option", "pex", "plugin")
        ):
            focus.append("short-ui-label")
        if len(source) >= 120 or any(marker in row_type for marker in ("help", "description", "desc", "tooltip")):
            focus.append("long-help-text")
        if len(source_targets.get(_normalized(source), set())) > 1:
            focus.append("conflicting-targets")
        group["SemanticFocus"] = focus

    return sorted(
        grouped.values(),
        key=lambda item: (
            str(item["Source"]).casefold(),
            str(item["Target"]).casefold(),
            str(item["Type"]).casefold(),
            str(item["Risk"]).casefold(),
            str(item["ContextKey"]).casefold(),
        ),
    )


_CONTEXT_ROLE_RE = re.compile(
    r"(?i)(?:^|[;|,/.:\\])(?:field|role|kind|type)?\s*[=:]?\s*"
    r"(?:label|name|title|help|description|desc|tooltip)(?=$|[;|,/.:\\])"
)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_OPPOSITE_TERMS = (
    frozenset(("enable", "disable")),
    frozenset(("open", "close")),
    frozenset(("show", "hide")),
    frozenset(("increase", "decrease")),
    frozenset(("yes", "no")),
    frozenset(("on", "off")),
)


def _context_anchor(group: dict[str, object]) -> str:
    context = _normalized(group.get("ContextKey"))
    context = _CONTEXT_ROLE_RE.sub(";", context).strip(" ;|,/.:\\")
    occurrences = group.get("Occurrences", [])
    first_file = ""
    if isinstance(occurrences, list) and occurrences and isinstance(occurrences[0], dict):
        first_file = _normalized(occurrences[0].get("File"))
    if not context or context in {"$", "root"}:
        return ""
    return f"{first_file}|{context}"


def _obviously_different_sources(left: str, right: str) -> bool:
    left_words = set(_WORD_RE.findall(left.casefold()))
    right_words = set(_WORD_RE.findall(right.casefold()))
    if not left_words or not right_words or left_words == right_words:
        return False
    if any(pair <= (left_words | right_words) and pair & left_words and pair & right_words for pair in _OPPOSITE_TERMS):
        return True
    return not (left_words & right_words)


def review_group_sections(groups: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Build compact cross-group summaries without discarding occurrence evidence."""
    sections: dict[str, list[dict[str, object]]] = {
        "source_target_conflicts": [],
        "suspicious_shared_targets": [],
        "label_help_pairs": [],
        "semantic_focus_high_risk": [],
    }

    by_source: dict[str, list[dict[str, object]]] = {}
    by_target: dict[str, list[dict[str, object]]] = {}
    by_anchor: dict[str, list[dict[str, object]]] = {}
    for group in groups:
        source = _normalized(group.get("Source"))
        target = _normalized(group.get("Target"))
        if source:
            by_source.setdefault(source, []).append(group)
        if target:
            by_target.setdefault(target, []).append(group)
        anchor = _context_anchor(group)
        if anchor:
            by_anchor.setdefault(anchor, []).append(group)

    for source, members in by_source.items():
        targets = {_normalized(member.get("Target")) for member in members}
        if len(targets) <= 1:
            continue
        sections["source_target_conflicts"].append(_section_record("source-conflict", source, members))

    for target, members in by_target.items():
        sources = sorted({_text(member.get("Source")) for member in members if _text(member.get("Source"))})
        if len(sources) <= 1 or not any(
            _obviously_different_sources(left, right)
            for index, left in enumerate(sources)
            for right in sources[index + 1 :]
        ):
            continue
        sections["suspicious_shared_targets"].append(_section_record("target-collision", target, members))

    for anchor, members in by_anchor.items():
        labels = [member for member in members if "short-ui-label" in member.get("SemanticFocus", [])]
        helps = [member for member in members if "long-help-text" in member.get("SemanticFocus", [])]
        if labels and helps:
            sections["label_help_pairs"].append(_section_record("label-help", anchor, [*labels, *helps]))

    semantic_buckets: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for group in groups:
        focus = sorted(str(value) for value in group.get("SemanticFocus", []))
        risk = _normalized(group.get("Risk"))
        if any(marker in risk for marker in ("high", "manual", "protected")):
            focus.append("high-risk")
        if focus:
            semantic_buckets.setdefault(tuple(sorted(set(focus))), []).append(group)
    for focus, members in sorted(semantic_buckets.items()):
        record = _section_record("semantic-focus", ",".join(focus), members)
        record["SemanticFocus"] = list(focus)
        sections["semantic_focus_high_risk"].append(record)
    return sections


def _section_record(prefix: str, identity: object, members: list[dict[str, object]]) -> dict[str, object]:
    unique_members = {str(member.get("GroupId", "")): member for member in members}
    ordered = [unique_members[key] for key in sorted(unique_members)]
    return {
        "GroupId": _stable_group_id(prefix, identity, *unique_members),
        "MemberGroupIds": [str(member.get("GroupId", "")) for member in ordered],
        "OccurrenceCount": sum(int(member.get("OccurrenceCount", 0) or 0) for member in ordered),
        "Sources": sorted({_text(member.get("Source")) for member in ordered if _text(member.get("Source"))}),
        "Targets": sorted({_text(member.get("Target")) for member in ordered if _text(member.get("Target"))}),
        "SemanticFocus": sorted(
            {
                str(focus)
                for member in ordered
                for focus in member.get("SemanticFocus", [])
            }
        ),
    }


def append_review_group_sections(lines: list[str], groups: list[dict[str, object]]) -> None:
    sections = review_group_sections(groups)
    definitions = (
        ("Source To Multiple Targets", "source_target_conflicts", "Source", "Targets"),
        ("Suspicious Shared Targets", "suspicious_shared_targets", "Target", "Sources"),
        ("Label And Help Pairs", "label_help_pairs", "", ""),
        ("Semantic Focus And High Risk", "semantic_focus_high_risk", "", ""),
    )
    for title, key, value_heading, values_heading in definitions:
        lines.extend(["", f"## {title}", ""])
        items = sections[key]
        if not items:
            lines.append("None.")
            continue
        if value_heading:
            lines.extend(
                [
                    f"| Summary group | Member review groups | Occurrences | {value_heading} | {values_heading} |",
                    "|---|---|---:|---|---|",
                ]
            )
        else:
            lines.extend(
                [
                    "| Summary group | Member review groups | Occurrences | Focus |",
                    "|---|---|---:|---|",
                ]
            )
        for item in items:
            prefix = (
                f"| {markdown_cell(item['GroupId'])} | {markdown_cell(', '.join(item['MemberGroupIds']))} | "
                f"{item['OccurrenceCount']} |"
            )
            if key == "source_target_conflicts":
                lines.append(
                    f"{prefix} {markdown_cell(' / '.join(item['Sources']))} | "
                    f"{markdown_cell(' / '.join(item['Targets']))} |"
                )
            elif key == "suspicious_shared_targets":
                lines.append(
                    f"{prefix} {markdown_cell(' / '.join(item['Targets']))} | "
                    f"{markdown_cell(' / '.join(item['Sources']))} |"
                )
            else:
                lines.append(
                    f"{prefix} {markdown_cell(', '.join(item['SemanticFocus']) or 'consistency')} |"
                )


def append_review_groups_table(
    lines: list[str],
    groups: list[dict[str, object]],
    *,
    kind_heading: str,
    target_heading: str,
    include_line: bool,
    cell: Callable[[object], str] = markdown_cell,
) -> None:
    lines.extend(
        [
            f"| Group ID | {kind_heading} | Risk | Context | Occurrences | Semantic focus | Representative evidence | Source | {target_heading} |",
            "|---|---|---|---|---:|---|---|---|---|",
        ]
    )
    for group in groups:
        occurrences = group["Occurrences"]
        assert isinstance(occurrences, list)
        references: list[str] = []
        for occurrence in occurrences[:5]:
            assert isinstance(occurrence, dict)
            reference = str(occurrence.get("File", ""))
            line = int(occurrence.get("Line", 0) or 0)
            if include_line and line:
                reference += f":{line}"
            if occurrence.get("Context"):
                reference += f" ({occurrence['Context']})"
            references.append(reference)
        lines.append(
            f"| {cell(group['GroupId'])} | {cell(group['Type'])} | {cell(group['Risk'])} | "
            f"{cell(group['ContextKey'])} | {group['OccurrenceCount']} | "
            f"{cell(', '.join(str(item) for item in group['SemanticFocus']) or 'standard')} | "
            f"{cell('; '.join(references))} | {cell(group['Source'])} | {cell(group['Target'])} |"
        )


def default_translation_context(
    mod_name: str,
    context: GameContext,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    groups = aggregate_source_rows(rows)
    return {
        "schema_version": 1,
        "status": "needs_model_analysis",
        "game_id": context.game_id,
        "game_display_name": context.display_name,
        "mod_name": mod_name,
        "source_items_sha256": source_rows_hash(rows),
        "source_item_count": len(source_evidence_rows(rows)),
        "unique_source_count": len(groups),
        "summary": "",
        "purpose": "",
        "features": [],
        "tone": "",
        "term_preferences": [],
        "ui_label_rules": [],
        "ambiguous_terms": [],
        "evidence_files": [],
        "confidence": "",
    }


def validate_translation_context(
    payload: dict[str, object],
    *,
    expected_game_id: str,
    expected_mod_name: str,
    expected_source_hash: str,
) -> list[str]:
    issues: list[str] = []
    if payload.get("schema_version") != 1:
        issues.append("translation context schema_version must be 1")
    if _text(payload.get("status")) != "complete":
        issues.append("translation context status must be complete")
    if _text(payload.get("game_id")) != expected_game_id:
        issues.append("translation context game_id does not match the workspace Game Profile")
    if _text(payload.get("mod_name")) != expected_mod_name:
        issues.append("translation context mod_name does not match")
    if _text(payload.get("source_items_sha256")) != expected_source_hash:
        issues.append("translation context source_items_sha256 is stale")
    for field in REQUIRED_CONTEXT_TEXT_FIELDS:
        if not _text(payload.get(field)):
            issues.append(f"translation context {field} must be non-empty")
    for field in REQUIRED_CONTEXT_LIST_FIELDS:
        value = payload.get(field)
        if not isinstance(value, list) or not any(_text(item) for item in value):
            issues.append(f"translation context {field} must contain at least one item")
    for field in ("term_preferences", "ui_label_rules", "ambiguous_terms"):
        if not isinstance(payload.get(field), list):
            issues.append(f"translation context {field} must be a list")
    return issues


def read_translation_context(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _packet_source_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    prefix = "- Source Items SHA256:"
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def validated_translation_context(
    root: Path,
    mod_name: str,
    context: GameContext,
) -> tuple[dict[str, object], list[str]]:
    context_path = root / "qa" / f"{mod_name}.translation_context.json"
    packet_path = root / "qa" / f"{mod_name}.translation_context_packet.md"
    payload = read_translation_context(context_path)
    expected_hash = _packet_source_hash(packet_path)
    if not expected_hash:
        issues = ["translation context packet is missing or has no source hash"]
    else:
        issues = validate_translation_context(
            payload,
            expected_game_id=context.game_id,
            expected_mod_name=mod_name,
            expected_source_hash=expected_hash,
        )
    if not issues:
        return payload, []
    sanitized = dict(payload)
    sanitized.update(
        {
            "status": "invalid",
            "summary": "",
            "purpose": "",
            "features": [],
            "tone": "",
            "term_preferences": [],
            "ui_label_rules": [],
            "ambiguous_terms": [],
            "confidence": "",
        }
    )
    return sanitized, issues


def write_translation_context_packet(
    root: Path,
    mod_name: str,
    rows: list[dict[str, object]],
    context: GameContext,
    packet_path: Path,
    context_path: Path,
) -> tuple[str, list[str]]:
    source_hash = source_rows_hash(rows)
    groups = aggregate_source_rows(rows)
    existing = read_translation_context(context_path)
    if not context_path.is_file() or _text(existing.get("status")) != "complete":
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            json.dumps(default_translation_context(mod_name, context, rows), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        existing = read_translation_context(context_path)
    validation_issues = validate_translation_context(
        existing,
        expected_game_id=context.game_id,
        expected_mod_name=mod_name,
        expected_source_hash=source_hash,
    )

    lines = [
        f"# Mod Translation Context Packet: {mod_name}",
        "",
        f"- Game: {game_display_label(context)}",
        f"- game_id: {context.game_id}",
        f"- Support level: {context.support_level}",
        f"- Source Items SHA256: {source_hash}",
        f"- Source occurrences: {len(source_evidence_rows(rows))}",
        f"- Unique source groups: {len(groups)}",
        f"- Context output: {context_path.relative_to(root).as_posix()}",
        "",
        "## Model Analysis Contract",
        "",
        "Analyze the source evidence before translation or semantic review, then complete the context JSON.",
        "",
        "- Treat the Game Profile above as authoritative. Never infer the game from the Mod name or file names.",
        "- Summarize what the Mod does, its player-facing features, UI tone, term choices, and ambiguous terms.",
        "- Base every conclusion on the listed source evidence. Do not invent lore, mechanics, or runtime behavior.",
        "- Set `status` to `complete` only after filling all required fields and keeping this source hash unchanged.",
        "- Use this context for translation and proofreading, but still review every semantic-focus or conflicting row.",
        "",
        "## Deduplicated Source Evidence",
        "",
        "| Group ID | Type | Risk | Context | Occurrences | Source | Representative evidence |",
        "|---|---|---|---|---:|---|---|",
    ]
    for group in groups:
        occurrences = group["Occurrences"]
        assert isinstance(occurrences, list)
        references = []
        for occurrence in occurrences[:5]:
            assert isinstance(occurrence, dict)
            reference = f"{occurrence['File']}:{occurrence['Line']}"
            if occurrence.get("Context"):
                reference += f" ({occurrence['Context']})"
            references.append(reference)
        lines.append(
            "| "
            + " | ".join(
                [
                    group["GroupId"],
                    markdown_cell(group["Type"]),
                    markdown_cell(group["Risk"]),
                    markdown_cell(group["ContextKey"]),
                    str(group["OccurrenceCount"]),
                    markdown_cell(group["Source"]),
                    markdown_cell("; ".join(references)),
                ]
            )
            + " |"
        )
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return source_hash, validation_issues
