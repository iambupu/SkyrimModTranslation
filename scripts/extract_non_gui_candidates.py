"""Collect non-GUI translation candidates from a prepared Mod workspace.

The output feeds coverage audits and translation packs. Binary and PSC scans are
discovery-only: they identify possible visible text but are not writeback
authority.
"""

import argparse
import json
import re
import string
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree

from capability_resolver import resolve_resource_capability
from game_context import GameContext
from project_paths import project_root, safe_file_name
from resource_model import ResourceDescriptor, classify_resource
from route_translation_task import current_game_context
from project_paths import is_under
from file_utils import read_text_auto_cp1252 as read_text, write_jsonl_sorted as write_jsonl
from project_paths import ensure_inside_or_exit as ensure_inside, relative_posix_strict as rel
from translation_candidate_shards import write_translation_candidate_shards


VISIBLE_XML_FILENAMES = {"info.xml", "moduleconfig.xml"}
VISIBLE_XML_DIRS = {"fomod"}
PROTECTED_PREFIXES = (
    "BL_",
    "BimLips",
    "BoS_",
    "PRJ_",
    "MuFacialExpressionExtended",
)
VISIBLE_MARKERS = (
    "Debug.Notification",
    "MessageBox",
    "ShowMessage",
    "Show(",
    "SetTextOptionValue",
    "SetInfoText",
    "SetTitleText",
    "SetMenuOptionValue",
)
VISIBLE_FIELD_NAMES = {
    "description",
    "desc",
    "displayname",
    "help",
    "label",
    "message",
    "name",
    "option",
    "pagedisplayname",
    "text",
    "title",
    "tooltip",
}
LOGIC_MARKERS = (
    "StorageUtil.",
    "JsonUtil.",
    "RegisterForModEvent",
    "UnregisterForModEvent",
    "HasIntValue",
    "GetIntValue",
    "SetIntValue",
    "HasFloatValue",
    "GetFloatValue",
    "SetFloatValue",
)
GLOBAL_PROTECTED_JSON_VALUE_KEYS = {
    "id",
    "scriptname",
    "script_name",
    "function",
    "form",
    "modname",
    "mod_name",
}
MCM_PROTECTED_JSON_VALUE_KEYS = GLOBAL_PROTECTED_JSON_VALUE_KEYS | {
    "source",
    "sourcetype",
    "source_type",
    "type",
    "params",
    "defaultvalue",
    "default_value",
    "min",
    "max",
    "step",
    "cursorfillmode",
    "cursor_fill_mode",
}
MCM_PATH_MARKERS = {"mcm"}
DATA_PATH_PREFIX = re.compile(
    r"^(?:scripts|interface|mcm|f4se|skse|meshes|textures|materials|sound|music|video|strings|seq|vis)/",
    re.IGNORECASE,
)


def descriptor_payload(descriptor: ResourceDescriptor) -> dict[str, object]:
    return {
        "relative_path": descriptor.relative_path.as_posix(),
        "category": descriptor.category,
        "subtype": descriptor.subtype,
        "container": descriptor.container,
        "extension": descriptor.extension,
        "capability": descriptor.capability,
        "traits": sorted(descriptor.traits),
    }


def add_descriptor(
    rows: list[dict],
    descriptor: ResourceDescriptor,
) -> list[dict]:
    payload = descriptor_payload(descriptor)
    for row in rows:
        row["descriptor"] = payload
    return rows


def config_manual_review_row(
    project_root: Path,
    path: Path,
    game_id: str,
) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "config-manual-review",
        "risk": "review",
        "reason": "structured-config-manual-review",
        "status": "manual",
        "game_id": game_id,
    }


def extract_config_comments(project_root: Path, path: Path) -> list[dict]:
    """Extract full-line INI/TOML comments without treating values as text."""
    markers = (";", "#") if path.suffix.casefold() == ".ini" else ("#",)
    rows: list[dict] = []
    for line_no, line in enumerate(read_text(path).splitlines(), 1):
        stripped = line.lstrip()
        marker = next((value for value in markers if stripped.startswith(value)), "")
        if not marker:
            continue
        source = stripped[len(marker) :].strip()
        if not source:
            continue
        risk, reason = classify_string(source, f"{path.suffix.casefold()} full-line comment")
        if reason == "identifier-like" and re.fullmatch(r"(?:[A-Z][a-z]+|[a-z]+):?", source):
            risk, reason = "candidate", "config-comment-heading"
        rows.append(
            {
                "file": rel(project_root, path),
                "line": line_no,
                "source": source,
                "target": "",
                "kind": "config-comment",
                "risk": risk,
                "reason": f"full-line-comment-{reason}",
                "comment_prefix": marker,
                "status": "candidate" if risk == "candidate" else "review",
            }
        )
    return rows


def protected_container_review_row(
    project_root: Path,
    path: Path,
    game_id: str,
) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "protected-container-manual-review",
        "risk": "protected",
        "reason": "profile-protected-container",
        "status": "manual",
        "game_id": game_id,
    }


def f4se_manual_review_row(
    project_root: Path,
    path: Path,
    game_id: str,
) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "f4se-manual-review",
        "risk": "review",
        "reason": "f4se-container-manual-only",
        "status": "manual",
        "game_id": game_id,
    }


def mcm_structured_extractor_handoff_row(
    project_root: Path,
    path: Path,
    game_id: str,
) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "mcm-structured-extractor-handoff",
        "risk": "review",
        "reason": "agent-structured-mcm-extractor-required",
        "status": "tool-mediated",
        "game_id": game_id,
    }


def plugin_capability_observation(
    project_root: Path,
    path: Path,
    game_id: str,
    *,
    blocked: bool,
    reason: str,
) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "plugin-capability-blocker" if blocked else "plugin-manual-review",
        "risk": "blocking" if blocked else "review",
        "reason": reason,
        "game_id": game_id,
    }


def _normalized_resource_traits(
    traits: Iterable[str],
    evidence: Mapping[str, Any] | None,
) -> frozenset[str]:
    values = set(traits)
    if evidence is not None:
        raw_traits = evidence.get("traits", ())
        if isinstance(raw_traits, (str, bytes)) or not isinstance(
            raw_traits,
            (list, tuple, set, frozenset),
        ):
            raise ValueError("Candidate evidence traits must be a collection of strings")
        values.update(raw_traits)
    if not all(isinstance(trait, str) and trait.strip() for trait in values):
        raise ValueError("Candidate traits must contain only non-empty strings")
    return frozenset(trait.strip().casefold() for trait in values)



def build_unique_translation_pack(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        source = row.get("source", "")
        if source not in grouped:
            grouped[source] = {
                "source": source,
                "target": "",
                "count": 0,
                "kinds": [],
                "examples": [],
                "notes": "",
            }
        entry = grouped[source]
        entry["count"] += 1
        kind = row.get("kind", "")
        if kind and kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        if len(entry["examples"]) < 5:
            example = {
                "file": row.get("file", ""),
                "line": row.get("line", row.get("json_path", row.get("xml_path", ""))),
                "reason": row.get("reason", ""),
            }
            entry["examples"].append(example)
    return sorted(grouped.values(), key=lambda item: (item["source"].lower(), item["count"]))



def classify_string(value: str, context: str) -> tuple[str, str]:
    # Prefer false negatives over false positives. Identifiers, file paths,
    # plugin names, and script keys must stay protected unless a later review
    # explicitly promotes them.
    stripped = value.strip()
    normalized_context = context.lower()
    if not stripped:
        return "skip", "empty"
    if not any(ch.isalpha() for ch in stripped):
        return "protected", "punctuation-or-symbol"
    if stripped.startswith("$"):
        return "protected", "translation-key"
    if "debug.trace" in normalized_context:
        return "protected", "debug-trace"
    if re.fullmatch(r"\{\d+\}\s*[A-Za-z%]+", stripped):
        return "protected", "format-string"
    if re.fullmatch(r"[A-Za-z0-9]+\s+[A-Za-z]:[A-Za-z0-9]+", stripped):
        return "protected", "ini-setting-name"
    if re.fullmatch(r"[A-Z][A-Z0-9 ]{2,}", stripped):
        return "protected", "brand-or-acronym"
    if re.fullmatch(r"\[[A-Za-z0-9_. -]+\]", stripped):
        return "protected", "brand-or-debug-prefix"
    if re.fullmatch(r"[A-Za-z0-9 ]+,\s+by\s+[A-Za-z0-9_ -]+", stripped, re.IGNORECASE):
        return "protected", "credit-or-theme-name"
    if re.fullmatch(r"ID\s+\d+\s+-\s+PRJ_[A-Za-z0-9_]+", stripped):
        return "protected", "morph-slot-identifier"
    if re.search(r"\.(esp|esm|esl|pex|psc|dll|exe|json|ini|xml|txt)$", stripped, re.IGNORECASE):
        return "protected", "file-name"
    if any(stripped.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return "protected", "internal-prefix"
    if "\\" in stripped or stripped.startswith(("./", "../", "/")) or DATA_PATH_PREFIX.search(stripped):
        return "protected", "path-like"
    if any(marker.lower() in normalized_context for marker in LOGIC_MARKERS):
        return "protected", "logic-context"
    if any(marker.lower() in normalized_context for marker in VISIBLE_MARKERS):
        return "candidate", "visible-api-context"
    context_tokens = set(re.findall(r"[A-Za-z]+", normalized_context))
    if context_tokens & VISIBLE_FIELD_NAMES:
        return "candidate", "visible-field-context"
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", stripped) and " " not in stripped:
        return "protected", "identifier-like"
    if " " in stripped and any(ch.isalpha() for ch in stripped):
        return "candidate", "human-readable"
    return "review", "uncertain"


def extract_interface_translation(project_root: Path, path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(read_text(path).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        if "\t" not in line:
            rows.append(
                {
                    "file": rel(project_root, path),
                    "line": line_no,
                    "source": line,
                    "kind": "interface-translation",
                    "risk": "review",
                    "reason": "missing-tab-separator",
                    "target": "",
                }
            )
            continue
        key, value = line.split("\t", 1)
        risk, reason = classify_string(value, line)
        rows.append(
            {
                "file": rel(project_root, path),
                "line": line_no,
                "key": key,
                "source": value,
                "kind": "interface-translation",
                "risk": risk,
                "reason": reason,
                "target": "",
            }
        )
    return rows


def extract_markdown(project_root: Path, path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(read_text(path).splitlines(), 1):
        source = line.strip()
        if not source:
            continue
        risk, reason = classify_string(source, line)
        rows.append(
            {
                "file": rel(project_root, path),
                "line": line_no,
                "source": source,
                "kind": "markdown-line",
                "risk": risk,
                "reason": reason,
                "target": "",
            }
        )
    return rows


def interface_translation_group(path: Path) -> tuple[Path, str, str] | None:
    stem = path.stem
    if "_" not in stem:
        return None
    base, language = stem.rsplit("_", 1)
    return path.parent, base.lower(), language.lower()


def select_target_interface_files(files: list[Path]) -> set[Path]:
    interface_files = [
        path
        for path in files
        if path.suffix.lower() == ".txt" and "translations" in [part.lower() for part in path.parts]
    ]
    grouped: dict[tuple[Path, str], dict[str, Path]] = {}
    passthrough: set[Path] = set()
    for path in interface_files:
        group = interface_translation_group(path)
        if group is None:
            passthrough.add(path)
            continue
        parent, base, language = group
        grouped.setdefault((parent, base), {})[language] = path

    selected = set(passthrough)
    for languages in grouped.values():
        if "chinese" in languages:
            selected.add(languages["chinese"])
        elif "english" in languages:
            selected.add(languages["english"])
        else:
            selected.update(languages.values())
    return selected


def walk_json_strings(value, path_parts=None):
    path_parts = path_parts or []
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_json_strings(child, path_parts + [str(key)])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_json_strings(child, path_parts + [str(index)])
    elif isinstance(value, str):
        yield path_parts, value


def is_mcm_json_path(project_root: Path, path: Path) -> bool:
    try:
        rel_parts = [part.lower() for part in path.relative_to(project_root).parts]
    except ValueError:
        rel_parts = [part.lower() for part in path.parts]
    return any(part in MCM_PATH_MARKERS for part in rel_parts)


def protected_json_value_reason(project_root: Path, path: Path, path_parts: list[str]) -> str:
    if not path_parts:
        return ""
    key_name = path_parts[-1].lower()
    protected_keys = MCM_PROTECTED_JSON_VALUE_KEYS if is_mcm_json_path(project_root, path) else GLOBAL_PROTECTED_JSON_VALUE_KEYS
    if key_name in protected_keys:
        return "protected-json-key"
    return ""


def extract_json(project_root: Path, path: Path) -> list[dict]:
    try:
        data = json.loads(read_text(path))
    except Exception as exc:
        return [
            {
                "file": rel(project_root, path),
                "line": 0,
                "source": "",
                "kind": "json",
                "risk": "review",
                "reason": f"json-parse-failed: {exc}",
                "target": "",
            }
        ]
    rows = []
    for path_parts, value in walk_json_strings(data):
        json_path = ".".join(path_parts)
        protected_reason = protected_json_value_reason(project_root, path, path_parts)
        if protected_reason:
            risk, reason = "protected", protected_reason
        else:
            risk, reason = classify_string(value, json_path)
        rows.append(
            {
                "file": rel(project_root, path),
                "json_path": json_path,
                "source": value,
                "kind": "json-string",
                "risk": risk,
                "reason": reason,
                "target": "",
            }
        )
    return rows


def extract_xml(project_root: Path, path: Path) -> list[dict]:
    try:
        root = ElementTree.fromstring(read_text(path))
    except Exception as exc:
        return [
            {
                "file": rel(project_root, path),
                "source": "",
                "kind": "xml",
                "risk": "review",
                "reason": f"xml-parse-failed: {exc}",
                "target": "",
            }
        ]
    rows = []
    for element in root.iter():
        tag = element.tag
        if element.text and element.text.strip():
            if tag.lower() in {"name", "modulename"}:
                risk, reason = "protected", "mod-display-name"
            else:
                risk, reason = classify_string(element.text, tag)
            rows.append(
                {
                    "file": rel(project_root, path),
                    "xml_path": tag,
                    "source": element.text,
                    "kind": "xml-text",
                    "risk": risk,
                    "reason": reason,
                    "target": "",
                }
            )
        for attr_name, attr_value in element.attrib.items():
            xml_path = f"{tag}@{attr_name}"
            if attr_name.lower() in {"name", "file", "path"} and tag.lower() in {"plugin", "file"}:
                risk, reason = "protected", "dependency-or-file-attribute"
            else:
                risk, reason = classify_string(attr_value, xml_path)
            rows.append(
                {
                    "file": rel(project_root, path),
                    "xml_path": xml_path,
                    "source": attr_value,
                    "kind": "xml-attribute",
                    "risk": risk,
                    "reason": reason,
                    "target": "",
                }
            )
    return rows


def is_visible_xml_path(project_root: Path, path: Path) -> bool:
    rel_parts = [part.lower() for part in path.relative_to(project_root).parts]
    if any(part in {"meshes", "textures", "facegendata"} for part in rel_parts):
        return False
    if path.name.lower() in VISIBLE_XML_FILENAMES and any(part in VISIBLE_XML_DIRS for part in rel_parts):
        return True
    if any(part in {"interface", "mcm"} for part in rel_parts):
        return True
    return False


def extract_psc(project_root: Path, path: Path) -> list[dict]:
    # PSC is read for context only. The workflow never rewrites or recompiles
    # source scripts, even when a string literal looks player-visible.
    rows = []
    pattern = re.compile(r'"((?:[^"\\]|\\.)*)"')
    lines = read_text(path).splitlines()
    for line_no, line in enumerate(lines, 1):
        stripped_line = line.strip()
        if stripped_line.startswith(";"):
            for match in pattern.finditer(line):
                raw_value = match.group(1)
                value = bytes(raw_value, "utf-8").decode("unicode_escape", errors="replace")
                rows.append(
                    {
                        "file": rel(project_root, path),
                        "line": line_no,
                        "source": value,
                        "kind": "psc-string-literal",
                        "risk": "skip",
                        "reason": "commented-out",
                        "context": stripped_line,
                        "target": "",
                    }
                )
            continue
        for match in pattern.finditer(line):
            raw_value = match.group(1)
            value = bytes(raw_value, "utf-8").decode("unicode_escape", errors="replace")
            risk, reason = classify_string(value, line)
            if risk == "candidate" and reason == "human-readable":
                risk = "review"
                reason = "psc-human-readable-needs-visible-api-or-pex-confirmation"
            rows.append(
                {
                    "file": rel(project_root, path),
                    "line": line_no,
                    "source": value,
                    "kind": "psc-string-literal",
                    "risk": risk,
                    "reason": reason,
                    "context": line.strip(),
                    "target": "",
                }
            )
    return rows


def printable_binary_strings(data: bytes, min_len: int = 4) -> list[str]:
    allowed = set(bytes(string.printable, "ascii")) - {0x0b, 0x0c}
    values = []
    current = bytearray()
    for byte in data:
        if byte in allowed and byte not in (0x00,):
            current.append(byte)
        else:
            if len(current) >= min_len:
                values.append(current.decode("ascii", errors="ignore"))
            current.clear()
    if len(current) >= min_len:
        values.append(current.decode("ascii", errors="ignore"))
    return values


def extract_binary_scan(project_root: Path, path: Path) -> list[dict]:
    values = printable_binary_strings(path.read_bytes())
    rows = []
    seen = set()
    for value in values:
        stripped = value.strip()
        if stripped in seen:
            continue
        seen.add(stripped)
        risk, reason = classify_string(stripped, "")
        if risk == "skip":
            continue
        rows.append(
            {
                "file": rel(project_root, path),
                "source": stripped,
                "kind": f"{path.suffix.lower()[1:]}-binary-string-scan",
                "risk": "review" if risk == "candidate" else risk,
                "reason": f"binary-scan-{reason}",
                "target": "",
            }
        )
    return rows


def add_game_id(rows: list[dict], game_id: str) -> list[dict]:
    for row in rows:
        row["game_id"] = game_id
    return rows


def localized_string_table_blocker(project_root: Path, path: Path, game_id: str) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "localized-string-table-blocker",
        "risk": "blocking",
        "reason": "missing-string-table-adapter",
        "status": "blocked",
        "evidence": "string table adapter missing; payload not decoded",
        "game_id": game_id,
    }


def localized_string_table_handoff(project_root: Path, path: Path, game_id: str) -> dict:
    return {
        "file": rel(project_root, path),
        "source": "",
        "target": "",
        "kind": "localized-string-table-tool-handoff",
        "risk": "review",
        "reason": "controlled-string-table-tool-required",
        "status": "tool-mediated",
        "evidence": "string table routed to controlled tool workflow; payload not decoded",
        "game_id": game_id,
    }


def extract_file_observations(
    root: Path,
    workspace_dir: Path,
    path: Path,
    context: GameContext,
    *,
    target_interface_files: set[Path],
    traits: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
    descriptor: ResourceDescriptor | None = None,
) -> tuple[list[dict], bool]:
    relative_resource_path = path.relative_to(workspace_dir)
    supplied_traits = _normalized_resource_traits(traits, evidence)
    if descriptor is None:
        resource = classify_resource(
            context,
            relative_resource_path,
            traits=supplied_traits,
        )
    else:
        if not isinstance(descriptor, ResourceDescriptor):
            raise TypeError("descriptor must be a ResourceDescriptor")
        if descriptor.relative_path != relative_resource_path:
            raise ValueError(
                "descriptor.relative_path must match path relative to workspace_dir"
            )
        resource = replace(
            descriptor,
            traits=frozenset((*descriptor.traits, *supplied_traits)),
        )

    lower_parts = [part.casefold() for part in path.parts]
    file_rows: list[dict] = []
    skipped_resource_xml = False
    if resource.container == "protected":
        file_rows = [protected_container_review_row(root, path, context.game_id)]
        skipped_resource_xml = resource.extension == ".xml"
    elif resource.container == "f4se":
        if resource.extension in {".ini", ".toml"}:
            file_rows = add_game_id(extract_config_comments(root, path), context.game_id)
            file_rows.append(config_manual_review_row(root, path, context.game_id))
        elif resource.extension == ".json":
            file_rows = [config_manual_review_row(root, path, context.game_id)]
        else:
            file_rows = [f4se_manual_review_row(root, path, context.game_id)]
    elif resource.container == "mcm" and resource.extension in {".json", ".ini"}:
        if resource.extension == ".ini":
            file_rows = add_game_id(extract_config_comments(root, path), context.game_id)
        file_rows.append(mcm_structured_extractor_handoff_row(root, path, context.game_id))
    elif resource.container == "mcm" and resource.extension == ".toml":
        file_rows = add_game_id(extract_config_comments(root, path), context.game_id)
        file_rows.append(config_manual_review_row(root, path, context.game_id))
    elif resource.extension == ".txt" and "translations" in lower_parts:
        if path not in target_interface_files:
            return [], False
        file_rows = add_game_id(
            extract_interface_translation(root, path),
            context.game_id,
        )
    elif resource.category == "string_table":
        read = resolve_resource_capability(context, resource, "read")
        if read.supported:
            file_rows = [localized_string_table_handoff(root, path, context.game_id)]
        else:
            file_rows = [localized_string_table_blocker(root, path, context.game_id)]
    elif resource.subtype == "config_text":
        file_rows = add_game_id(extract_config_comments(root, path), context.game_id)
        file_rows.append(config_manual_review_row(root, path, context.game_id))
    elif resource.extension == ".md":
        file_rows = add_game_id(extract_markdown(root, path), context.game_id)
    elif resource.extension == ".json":
        file_rows = add_game_id(extract_json(root, path), context.game_id)
    elif resource.extension == ".xml":
        if is_visible_xml_path(root, path):
            file_rows = add_game_id(extract_xml(root, path), context.game_id)
        else:
            skipped_resource_xml = True
    elif resource.subtype == "papyrus.source":
        file_rows = add_game_id(extract_psc(root, path), context.game_id)
    elif resource.category == "plugin":
        read = resolve_resource_capability(context, resource, "read")
        if read.supported:
            file_rows = add_game_id(extract_binary_scan(root, path), context.game_id)
        else:
            inventory = resolve_resource_capability(context, resource, "inventory")
            file_rows = [
                plugin_capability_observation(
                    root,
                    path,
                    context.game_id,
                    blocked=not inventory.supported,
                    reason=read.reason,
                )
            ]
    elif resource.subtype == "papyrus.binary":
        file_rows = add_game_id(extract_binary_scan(root, path), context.game_id)

    return add_descriptor(file_rows, resource), skipped_resource_xml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--workspace-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    work_root = root / "work" / "extracted_mods"
    mod_name = safe_file_name(args.mod_name.strip())

    if args.workspace_dir:
        workspace_dir = Path(args.workspace_dir)
        if not workspace_dir.is_absolute():
            workspace_dir = root / workspace_dir
        workspace_dir = workspace_dir.resolve()
        if not mod_name:
            mod_name = safe_file_name(workspace_dir.name)
    elif mod_name:
        workspace_dir = (work_root / mod_name).resolve()
    else:
        candidates = sorted(item for item in work_root.iterdir() if item.is_dir()) if work_root.is_dir() else []
        if len(candidates) != 1:
            raise SystemExit(f"Pass --mod-name or --workspace-dir. Found {len(candidates)} extracted workspaces.")
        workspace_dir = candidates[0].resolve()
        mod_name = safe_file_name(workspace_dir.name)

    if not mod_name:
        raise SystemExit("ModName cannot be empty after sanitization.")
    if not workspace_dir.is_dir():
        raise SystemExit(f"WorkspaceDir does not exist: {workspace_dir}")
    ensure_inside(workspace_dir, root)
    if not is_under(workspace_dir, work_root):
        raise SystemExit(f"WorkspaceDir must be under work/extracted_mods: {workspace_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else root / "out" / mod_name / "non_gui_exports"
    report_path = Path(args.report_path) if args.report_path else root / "out" / mod_name / "qa" / "non_gui_extraction_report.md"
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    if not report_path.is_absolute():
        report_path = root / report_path
    output_dir = output_dir.resolve()
    report_path = report_path.resolve()
    ensure_inside(output_dir, root)
    ensure_inside(report_path, root)
    out_root = root / "out"
    if not is_under(output_dir, out_root):
        raise SystemExit(f"OutputDir must be under out/: {output_dir}")
    if not is_under(report_path, out_root):
        raise SystemExit(f"ReportPath must be under out/: {report_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    context = current_game_context(root)
    rows: list[dict] = []
    skipped_resource_xml: list[str] = []
    files = [path for path in workspace_dir.rglob("*") if path.is_file()]
    target_interface_files = select_target_interface_files(files)
    for path in files:
        file_rows, skipped_xml = extract_file_observations(
            root,
            workspace_dir,
            path,
            context,
            target_interface_files=target_interface_files,
        )
        rows.extend(file_rows)
        if skipped_xml:
            skipped_resource_xml.append(rel(root, path))

    # Keep protected and manual-review buckets beside candidates; they are the
    # audit trail for why a string was intentionally not translated.
    candidates = [row for row in rows if row.get("risk") == "candidate"]
    blockers = [row for row in rows if row.get("risk") == "blocking"]
    protected = [row for row in rows if row.get("risk") == "protected"]
    review = [row for row in rows if row.get("risk") == "review"]

    write_jsonl(output_dir / "all_string_observations.jsonl", rows)
    write_jsonl(output_dir / "translation_candidates.jsonl", candidates + blockers)
    unique_candidates = build_unique_translation_pack(candidates)
    unique_candidates_path = output_dir / "translation_candidates_unique.jsonl"
    write_jsonl(unique_candidates_path, unique_candidates)
    translation_shards = write_translation_candidate_shards(
        root=root,
        mod_name=mod_name,
        game_id=context.game_id,
        source_jsonl=unique_candidates_path,
        rows=unique_candidates,
    )
    write_jsonl(output_dir / "blocking_or_unsupported_inputs.jsonl", blockers)
    write_jsonl(output_dir / "protected_or_logic_strings.jsonl", protected)
    write_jsonl(output_dir / "manual_review_strings.jsonl", review)

    by_kind: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        by_risk[row["risk"]] = by_risk.get(row["risk"], 0) + 1

    report = [
        "# Non-GUI Extraction Report",
        "",
        f"- GameId: {context.game_id}",
        f"- ModName: {mod_name}",
        f"- Workspace: {rel(root, workspace_dir)}",
        f"- OutputDir: {rel(root, output_dir)}",
        f"- Files scanned: {len(files)}",
        f"- String observations: {len(rows)}",
        f"- Translation candidates: {len(candidates)}",
        f"- Blocking inputs: {len(blockers)}",
        f"- Unique translation candidates: {len(unique_candidates)}",
        f"- Translation candidate shards: {translation_shards['shard_count']}",
        f"- Maximum rows per translation shard: {translation_shards['translation_batch_rows']}",
        f"- Translation shard index: work/shards/{mod_name}/translation_candidates/index.json",
        f"- Protected or logic strings: {len(protected)}",
        f"- Manual review strings: {len(review)}",
        f"- Resource XML files skipped: {len(skipped_resource_xml)}",
        "",
        "## Counts By Kind",
        "",
    ]
    for key in sorted(by_kind):
        report.append(f"- {key}: {by_kind[key]}")
    report.extend(["", "## Counts By Risk", ""])
    for key in sorted(by_risk):
        report.append(f"- {key}: {by_risk[key]}")
    report.extend(["", "## Resource XML Skipped", ""])
    if skipped_resource_xml:
        report.extend(f"- {item}" for item in skipped_resource_xml[:200])
        if len(skipped_resource_xml) > 200:
            report.append(f"- ... {len(skipped_resource_xml) - 200} more")
    else:
        report.append("No resource XML files were skipped.")
    report.extend(
        [
            "",
            "## Safety",
            "",
            "- This extraction is read-only.",
            "- ESP/ESM/ESL and PEX binary files are scanned only for candidate discovery; this is not a safe writeback method.",
            "- PSC files are read only for context and are not rewritten or compiled.",
            "- Outputs stay under the project out/<ModName>/ tree.",
            "",
            "## Scope Notes",
            "",
            "- This report lists non-GUI candidate discovery, not final translation approval.",
            "- ESP/ESM/ESL and PEX binary string scans are discovery aids only; authoritative writeback evidence comes from controlled project-local tool outputs and verification reports.",
            "- PSC strings are read only for context and candidate discovery; PSC recompilation is not used.",
            "- XML under Meshes, Textures, and FaceGenData is treated as resource metadata and is not translated.",
        ]
    )
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Non-GUI extraction report: {report_path}")
    print(f"Translation candidates: {len(candidates)}")
    print(f"Manual review strings: {len(review)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
