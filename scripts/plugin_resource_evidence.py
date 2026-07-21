"""Structured plugin traits and resource capability evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from capability_resolver import CapabilityDecision
from file_utils import (
    create_regular_directory_under,
    is_reparse_point,
    sha256_file,
    validate_regular_path_under,
)
from game_context import GameContext
from project_paths import safe_file_name
from resource_model import ResourceDescriptor, classify_resource


TRAIT_FIELDS = (
    "localized",
    "light_by_extension",
    "light_by_header",
    "light_context",
    "contains_unsupported_light_formids",
)
HEADER_DEPENDENT_TRAIT_FIELDS = (
    "localized",
    "light_by_header",
    "light_context",
    "contains_unsupported_light_formids",
)
RESOURCE_TRAIT_REPORT_FIELDS = {
    "localized": ("localized",),
    "light": ("light_by_header",),
    "contains_unsupported_light_formids": (
        "contains_unsupported_light_formids",
    ),
}
MAX_REPORT_BYTES = 1024 * 1024
MAX_MASTER_STYLE_CONTEXT_BYTES = 4 * 1024 * 1024
_TRAIT_LINE = re.compile(
    r"(?mi)^-[ \t]*(localized|light_by_extension|light_by_header|"
    r"light_context|contains_unsupported_light_formids):[ \t]*([^\r\n]*?)[ \t]*$"
)
_REPORT_VALUE = re.compile(r"(?mi)^-\s*([^:\r\n]+):\s*(.*?)\s*$")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
PLUGIN_REPORT_SUCCESS_STATUS = "ready"
PLUGIN_REPORT_FAILURE_STATUSES = frozenset({"blocked", "error", "failed"})
MASTER_STYLE_ERROR_CODES = (
    "master_style_unknown",
    "master_style_evidence_stale",
    "master_style_conflict",
)
TES4_RECORD_HEADER_SIZE = 24
TES4_LIGHT_FLAG = 0x00000200


def plugin_artifact_key(mod_name: str, relative_plugin: Path) -> str:
    normalized = relative_plugin.as_posix()
    if relative_plugin.anchor or not relative_plugin.parts or ".." in relative_plugin.parts:
        raise ValueError(f"Plugin relative path is not canonical: {relative_plugin}")
    digest = hashlib.sha256(
        f"{mod_name}\0{normalized.casefold()}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{safe_file_name(mod_name)}.{safe_file_name(relative_plugin.name)}.{digest}"


@dataclass(frozen=True)
class PluginReportTraits:
    localized: bool | None = None
    light_by_extension: bool | None = None
    light_by_header: bool | None = None
    light_context: bool | None = None
    contains_unsupported_light_formids: bool | None = None

    def resource_traits(self) -> frozenset[str]:
        traits: set[str] = set()
        if self.localized is True:
            traits.add("localized")
        if (
            self.light_by_extension is True
            or self.light_by_header is True
            or self.light_context is True
        ):
            traits.add("light")
        if self.contains_unsupported_light_formids is True:
            traits.add("contains_unsupported_light_formids")
        return frozenset(traits)

    def as_report_values(self) -> dict[str, str]:
        return {
            field: _format_trait(getattr(self, field))
            for field in TRAIT_FIELDS
        }

    def unknown_header_fields(self) -> tuple[str, ...]:
        return tuple(
            field
            for field in HEADER_DEPENDENT_TRAIT_FIELDS
            if getattr(self, field) is None
        )


def required_known_plugin_trait_fields(context: GameContext) -> tuple[str, ...]:
    trait_caps = context.resource_model.trait_level_caps.get("plugin_text", {})
    required = {
        field
        for trait in trait_caps
        for field in RESOURCE_TRAIT_REPORT_FIELDS.get(trait, ())
    }
    return tuple(field for field in TRAIT_FIELDS if field in required)


def unknown_write_plugin_trait_fields(
    context: GameContext,
    report_traits: PluginReportTraits,
) -> tuple[str, ...]:
    return tuple(
        field
        for field in required_known_plugin_trait_fields(context)
        if getattr(report_traits, field) is None
    )


@dataclass(frozen=True)
class PluginReportIdentity:
    game_id: str
    operation: str
    input_plugin: str
    input_sha256: str


@dataclass(frozen=True)
class PluginPostVerifyEvidence:
    translation_rows_verified: int
    blocking_issues: int


@dataclass(frozen=True)
class PluginMasterStyleContextEvidence:
    path: Path | None
    sha256: str
    light_context: bool


def _parse_trait(value: str) -> bool | None:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "unknown":
        return None
    raise ValueError(f"Invalid plugin trait value: {value!r}")


def _format_trait(value: bool | None) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def read_plugin_report_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_REPORT_BYTES:
        raise ValueError(f"Plugin adapter report exceeds {MAX_REPORT_BYTES} bytes: {path}")
    return path.read_text(encoding="utf-8-sig")


def plugin_report_error_code(path: Path) -> str:
    try:
        text = read_plugin_report_text(path)
    except (OSError, UnicodeError, ValueError):
        return ""
    for code in MASTER_STYLE_ERROR_CODES:
        if re.search(rf"(?<![a-z0-9_]){re.escape(code)}(?![a-z0-9_])", text):
            return code
    return ""


def _read_plugin_small_flag(path: Path, *, label: str) -> bool:
    with path.open("rb") as handle:
        header = handle.read(TES4_RECORD_HEADER_SIZE)
    if len(header) != TES4_RECORD_HEADER_SIZE or header[:4] != b"TES4":
        raise ValueError(f"{label} does not start with a complete TES4 header: {path}")
    flags = int.from_bytes(header[8:12], byteorder="little", signed=False)
    return bool(flags & TES4_LIGHT_FLAG)


def _style_from_header(path: Path, small_flag: bool) -> str:
    return "light" if path.suffix.casefold() == ".esl" or small_flag else "full"


def read_plugin_report_traits(path: Path) -> PluginReportTraits:
    text = read_plugin_report_text(path)
    raw_values: dict[str, list[str]] = {field: [] for field in TRAIT_FIELDS}
    for match in _TRAIT_LINE.finditer(text):
        field = match.group(1).casefold()
        raw_values[field].append(match.group(2))

    missing = [
        field
        for field, matches in raw_values.items()
        if field != "light_context" and not matches
    ]
    if missing:
        raise ValueError(
            f"Missing plugin trait fields ({', '.join(missing)}): {path}"
        )

    explicit_light_context = raw_values.pop("light_context")
    values: dict[str, bool | None] = {}
    for field, matches in raw_values.items():
        if len(matches) != 1:
            raise ValueError(f"Duplicate plugin trait field {field}: {path}")
        try:
            values[field] = _parse_trait(matches[0])
        except ValueError as exc:
            raise ValueError(
                f"Invalid plugin trait value for {field}: {matches[0]!r}: {path}"
            ) from exc
    context_path = _strict_report_value(path, text, "Master-style context")
    context_sha256 = _strict_report_value(path, text, "Master-style context SHA256")
    context_absent = context_path == "<none>" and context_sha256 == "<none>"
    if (context_path == "<none>") != (context_sha256 == "<none>"):
        raise ValueError(f"Plugin master-style context path/hash mismatch: {path}")
    if explicit_light_context:
        if len(explicit_light_context) != 1:
            raise ValueError(f"Duplicate plugin trait field light_context: {path}")
        try:
            values["light_context"] = _parse_trait(explicit_light_context[0])
        except ValueError as exc:
            raise ValueError(
                "Invalid plugin trait value for light_context: "
                f"{explicit_light_context[0]!r}: {path}"
            ) from exc
    else:
        values["light_context"] = not context_absent
    return PluginReportTraits(**values)


def validate_plugin_master_style_context(
    report_path: Path,
    *,
    project_root: Path,
    expected_input: Path,
    expected_game: str,
    sha256_resolver: Callable[[Path], str] = sha256_file,
) -> PluginMasterStyleContextEvidence:
    text = read_plugin_report_text(report_path)
    report_traits = read_plugin_report_traits(report_path)
    raw_path = _strict_report_value(report_path, text, "Master-style context")
    raw_sha256 = _strict_report_value(report_path, text, "Master-style context SHA256")
    if raw_path == "<none>" or raw_sha256 == "<none>":
        if raw_path != "<none>" or raw_sha256 != "<none>":
            raise ValueError(
                f"Plugin master-style context path/hash mismatch: {report_path}"
            )
        if report_traits.light_context is not False:
            raise ValueError("Plugin report light_context is inconsistent")
        if (
            report_traits.light_by_extension is True
            or report_traits.light_by_header is True
        ):
            raise ValueError(
                "Light plugin report is missing required master-style context evidence"
            )
        return PluginMasterStyleContextEvidence(None, "", False)
    if _SHA256.fullmatch(raw_sha256) is None:
        raise ValueError(
            f"Plugin master-style context SHA256 is invalid: {raw_sha256!r}"
        )

    normalized = raw_path.replace("\\", "/")
    parsed = PurePosixPath(normalized)
    if (
        parsed.is_absolute()
        or bool(Path(normalized).drive)
        or ".." in parsed.parts
        or parsed.as_posix() != normalized
    ):
        raise ValueError(f"Plugin master-style context path is not canonical: {raw_path!r}")
    root = project_root.resolve(strict=True)
    context_root = root / "work" / "plugin_context"
    context_path = validate_regular_evidence_path_under(
        root.joinpath(*parsed.parts),
        context_root,
        kind="file",
        label="Plugin master-style context",
    )
    actual_sha256 = sha256_resolver(context_path)
    if raw_sha256.casefold() != actual_sha256:
        raise ValueError(
            "Plugin master-style context SHA256 mismatch: "
            f"expected {actual_sha256}, found {raw_sha256}"
        )
    if context_path.stat().st_size > MAX_MASTER_STYLE_CONTEXT_BYTES:
        raise ValueError(
            "Plugin master-style context exceeds "
            f"{MAX_MASTER_STYLE_CONTEXT_BYTES} bytes: {context_path}"
        )
    try:
        payload = json.loads(context_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Plugin master-style context is invalid JSON: {context_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Plugin master-style context schema_version must be 1")
    if payload.get("game_id") != expected_game:
        raise ValueError("Plugin master-style context game_id mismatch")

    input_path = expected_input.resolve(strict=True)
    try:
        expected_relative = input_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Expected plugin input is outside project root: {input_path}") from exc
    if payload.get("plugin") != input_path.name:
        raise ValueError("Plugin master-style context plugin identity mismatch")
    if payload.get("input_path") != expected_relative:
        raise ValueError("Plugin master-style context input_path mismatch")
    if str(payload.get("input_sha256", "")).casefold() != sha256_resolver(input_path):
        raise ValueError("Plugin master-style context input_sha256 mismatch")

    current_style = payload.get("current_style")
    if current_style not in {"full", "light"}:
        raise ValueError("Plugin master-style context current_style is invalid")
    if not str(payload.get("current_evidence_source", "")).strip():
        raise ValueError("Plugin master-style context current evidence is empty")

    def validate_inspected_evidence(
        *,
        label: str,
        inspected_path: object,
        inspected_sha256: object,
        small_flag: object,
        allow_missing: bool,
    ) -> tuple[Path | None, bool | None]:
        if inspected_path is None and inspected_sha256 is None:
            if small_flag is not None:
                raise ValueError(
                    f"Plugin master-style small_flag requires inspected evidence for {label}"
                )
            if not allow_missing:
                raise ValueError(f"Plugin master-style inspected evidence is missing for {label}")
            return None, None
        if not isinstance(inspected_path, str) or not isinstance(inspected_sha256, str):
            raise ValueError(f"Plugin master-style inspected path/hash mismatch for {label}")
        if not isinstance(small_flag, bool):
            raise ValueError(f"Plugin master-style small_flag is missing for {label}")
        inspected_normalized = inspected_path.replace("\\", "/")
        inspected_parsed = PurePosixPath(inspected_normalized)
        if (
            inspected_parsed.is_absolute()
            or bool(Path(inspected_normalized).drive)
            or ".." in inspected_parsed.parts
            or inspected_parsed.as_posix() != inspected_normalized
            or _SHA256.fullmatch(inspected_sha256) is None
        ):
            raise ValueError(f"Plugin master-style inspected evidence is invalid for {label}")
        inspected = validate_regular_evidence_path_under(
            root.joinpath(*inspected_parsed.parts),
            root,
            kind="file",
            label=f"Plugin master-style inspected evidence for {label}",
        )
        if sha256_resolver(inspected) != inspected_sha256.casefold():
            raise ValueError(
                f"Plugin master-style inspected evidence hash mismatch for {label}"
            )
        actual_small_flag = _read_plugin_small_flag(inspected, label=label)
        if actual_small_flag is not small_flag:
            raise ValueError(
                f"Plugin master-style small_flag conflicts with inspected header for {label}"
            )
        return inspected, actual_small_flag

    current_inspected, current_small_flag = validate_inspected_evidence(
        label="current plugin",
        inspected_path=payload.get("current_inspected_path"),
        inspected_sha256=payload.get("current_inspected_sha256"),
        small_flag=payload.get("current_small_flag"),
        allow_missing=False,
    )
    assert current_inspected is not None and current_small_flag is not None
    if current_inspected != input_path:
        raise ValueError("Plugin master-style current inspected path does not match input plugin")
    if _style_from_header(current_inspected, current_small_flag) != current_style:
        raise ValueError("Plugin master-style current_style conflicts with inspected header")
    masters = payload.get("masters")
    if not isinstance(masters, list) or not all(isinstance(item, dict) for item in masters):
        raise ValueError("Plugin master-style context masters must be an array of objects")
    mod_keys: set[str] = set()
    light_context = current_style == "light"
    for item in masters:
        mod_key = str(item.get("mod_key", "")).strip()
        style = item.get("master_style")
        evidence_source = str(item.get("evidence_source", "")).strip()
        if not mod_key or mod_key.casefold() in mod_keys:
            raise ValueError("Plugin master-style context master identity is empty or duplicate")
        mod_keys.add(mod_key.casefold())
        if style not in {"full", "light"}:
            raise ValueError(f"Plugin master-style context style is invalid for {mod_key}")
        if not evidence_source:
            raise ValueError(f"Plugin master-style context evidence is empty for {mod_key}")
        light_context = light_context or style == "light"
        inspected, inspected_small_flag = validate_inspected_evidence(
            label=mod_key,
            inspected_path=item.get("inspected_path"),
            inspected_sha256=item.get("inspected_sha256"),
            small_flag=item.get("small_flag"),
            allow_missing=Path(mod_key).suffix.casefold() == ".esl",
        )
        if inspected is None:
            if style != "light":
                raise ValueError(
                    f"Plugin master-style extension-only evidence must be light for {mod_key}"
                )
        else:
            if inspected.name.casefold() != mod_key.casefold():
                raise ValueError(
                    f"Plugin master-style inspected identity mismatch for {mod_key}"
                )
            assert inspected_small_flag is not None
            if _style_from_header(inspected, inspected_small_flag) != style:
                raise ValueError(
                    f"Plugin master-style style conflicts with inspected header for {mod_key}"
                )
    if report_traits.light_context is not light_context:
        raise ValueError("Plugin report light_context is inconsistent")
    return PluginMasterStyleContextEvidence(context_path, actual_sha256, light_context)


def materialize_master_style_manifest(
    context: PluginMasterStyleContextEvidence,
    *,
    project_root: Path,
    destination: Path,
    expected_game: str,
    expected_plugin: str,
) -> Path | None:
    if context.path is None:
        return None
    payload = json.loads(context.path.read_text(encoding="utf-8-sig"))
    if payload.get("game_id") != expected_game or payload.get("plugin") != expected_plugin:
        raise ValueError("Plugin master-style context identity changed after validation")

    masters: list[dict[str, object]] = []
    for item in payload.get("masters", []):
        inspected_path = item.get("inspected_path")
        if inspected_path is None:
            if (
                Path(str(item.get("mod_key", ""))).suffix.casefold() == ".esl"
                and item.get("master_style") == "light"
            ):
                continue
            raise ValueError(
                "Cannot materialize master-style manifest without inspected evidence for "
                f"{item.get('mod_key', '<unknown>')}"
            )
        masters.append(
            {
                "mod_key": item["mod_key"],
                "master_style": item["master_style"],
                "inspected_path": inspected_path,
                "inspected_sha256": item["inspected_sha256"],
                "small_flag": item["small_flag"],
            }
        )
    if not masters:
        return None

    root = project_root.resolve(strict=True)
    context_root = root / "work" / "plugin_context"
    if destination.suffix.casefold() != ".json":
        raise ValueError("Output master-style manifest must be a JSON file")
    parent = create_evidence_directory_under(
        destination.parent,
        context_root,
        label="Output master-style manifest directory",
    )
    target = parent / destination.name
    content = json.dumps(
        {
            "schema_version": 2,
            "game_id": expected_game,
            "plugin": expected_plugin,
            "masters": masters,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return target


def read_plugin_report_value(path: Path, field: str) -> str:
    text = read_plugin_report_text(path)
    matches = [
        value.strip()
        for name, value in _REPORT_VALUE.findall(text)
        if name.strip().casefold() == field.casefold()
    ]
    if not matches:
        return ""
    if len(set(matches)) != 1:
        raise ValueError(f"Conflicting plugin report values for {field}: {path}")
    return matches[0]


def _strict_report_value(path: Path, text: str, field: str) -> str:
    matches = [
        value.strip()
        for name, value in _REPORT_VALUE.findall(text)
        if name.strip().casefold() == field.casefold()
    ]
    if len(matches) != 1 or not matches[0]:
        raise ValueError(
            f"Plugin adapter report must contain exactly one non-empty {field!r}: {path}"
        )
    return matches[0]


def validate_plugin_report_identity(
    path: Path,
    *,
    project_root: Path,
    expected_input: Path,
    expected_game: str,
    expected_operation: str,
) -> PluginReportIdentity:
    text = read_plugin_report_text(path)
    game_id = _strict_report_value(path, text, "game_id")
    operation = _strict_report_value(path, text, "Operation")
    input_plugin = _strict_report_value(path, text, "Input plugin")
    input_sha256 = _strict_report_value(path, text, "Input SHA256")

    root = project_root.resolve(strict=True)
    plugin = expected_input.resolve(strict=True)
    try:
        expected_relative = plugin.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Expected plugin input is outside project root: {plugin}") from exc

    normalized_input = input_plugin.replace("\\", "/")
    parsed_input = PurePosixPath(normalized_input)
    if (
        parsed_input.is_absolute()
        or ".." in parsed_input.parts
        or parsed_input.as_posix() != normalized_input
    ):
        raise ValueError(f"Plugin report Input plugin is not canonical: {input_plugin!r}")
    if normalized_input.casefold() != expected_relative.casefold():
        raise ValueError(
            "Plugin report Input plugin mismatch: "
            f"expected {expected_relative!r}, found {input_plugin!r}"
        )
    if game_id.casefold() != expected_game.casefold():
        raise ValueError(
            f"Plugin report game_id mismatch: expected {expected_game!r}, found {game_id!r}"
        )
    if operation.casefold() != expected_operation.casefold():
        raise ValueError(
            "Plugin report Operation mismatch: "
            f"expected {expected_operation!r}, found {operation!r}"
        )
    if _SHA256.fullmatch(input_sha256) is None:
        raise ValueError(f"Plugin report Input SHA256 is invalid: {input_sha256!r}")
    actual_sha256 = sha256_file(plugin)
    if input_sha256.casefold() != actual_sha256:
        raise ValueError(
            "Plugin report Input SHA256 mismatch: "
            f"expected {actual_sha256}, found {input_sha256}"
        )
    return PluginReportIdentity(
        game_id=game_id,
        operation=operation.casefold(),
        input_plugin=expected_relative,
        input_sha256=actual_sha256,
    )


def validate_plugin_report_status(path: Path, *, return_code: int) -> str:
    text = read_plugin_report_text(path)
    status = _strict_report_value(path, text, "Status").casefold()
    allowed = {PLUGIN_REPORT_SUCCESS_STATUS, *PLUGIN_REPORT_FAILURE_STATUSES}
    if status not in allowed:
        raise ValueError(
            f"Plugin adapter report Status is invalid: {status!r}: {path}"
        )
    if status == PLUGIN_REPORT_SUCCESS_STATUS and return_code != 0:
        raise ValueError(
            "Plugin adapter report Status 'ready' is inconsistent with "
            f"return code {return_code}: {path}"
        )
    if status in PLUGIN_REPORT_FAILURE_STATUSES and return_code == 0:
        raise ValueError(
            f"Plugin adapter report Status {status!r} is inconsistent with "
            f"return code 0: {path}"
        )
    return status


def validate_plugin_report_output(
    path: Path,
    *,
    project_root: Path,
    expected_output: Path,
) -> tuple[str, str]:
    text = read_plugin_report_text(path)
    output_plugin = _strict_report_value(path, text, "Output plugin")
    output_sha256 = _strict_report_value(path, text, "Output SHA256")

    root = project_root.resolve(strict=True)
    output = expected_output.resolve(strict=True)
    try:
        expected_relative = output.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Expected plugin output is outside project root: {output}") from exc

    normalized_output = output_plugin.replace("\\", "/")
    parsed_output = PurePosixPath(normalized_output)
    if (
        parsed_output.is_absolute()
        or ".." in parsed_output.parts
        or parsed_output.as_posix() != normalized_output
    ):
        raise ValueError(f"Plugin report Output plugin is not canonical: {output_plugin!r}")
    if normalized_output.casefold() != expected_relative.casefold():
        raise ValueError(
            "Plugin report Output plugin mismatch: "
            f"expected {expected_relative!r}, found {output_plugin!r}"
        )
    if _SHA256.fullmatch(output_sha256) is None:
        raise ValueError(f"Plugin report Output SHA256 is invalid: {output_sha256!r}")
    actual_sha256 = sha256_file(output)
    if output_sha256.casefold() != actual_sha256:
        raise ValueError(
            "Plugin report Output SHA256 mismatch: "
            f"expected {actual_sha256}, found {output_sha256}"
        )
    return expected_relative, actual_sha256


def validate_regular_evidence_path_under(
    path: Path,
    allowed_root: Path,
    *,
    kind: str,
    label: str,
) -> Path:
    return validate_regular_path_under(
        path,
        allowed_root,
        kind=kind,
        label=label,
    )


def discover_regular_plugin_files(
    root: Path,
    extensions: set[str] | frozenset[str],
    *,
    label: str,
) -> list[Path]:
    """Discover plugin files without following link-like entries before validation."""
    validated_root = validate_regular_evidence_path_under(
        root,
        root,
        kind="directory",
        label=f"{label} root",
    )
    plugins: list[Path] = []
    pending = [validated_root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                candidate = Path(entry.path)
                entry_stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or is_reparse_point(entry_stat):
                    raise ValueError(
                        f"{label} path contains a symlink, junction, or reparse point: "
                        f"{candidate}"
                    )
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(candidate)
                    continue
                if candidate.suffix.casefold() not in extensions:
                    continue
                plugins.append(
                    validate_regular_evidence_path_under(
                        candidate,
                        validated_root,
                        kind="file",
                        label=label,
                    )
                )
    return sorted(
        plugins,
        key=lambda item: item.relative_to(validated_root).as_posix().casefold(),
    )


def create_evidence_directory_under(
    path: Path,
    allowed_root: Path,
    *,
    label: str,
) -> Path:
    """Create a directory one component at a time without following reparse parents."""
    return create_regular_directory_under(path, allowed_root, label=label)


def _validate_report_path_and_hash(
    report_path: Path,
    text: str,
    *,
    project_root: Path,
    path_field: str,
    hash_field: str,
    expected_path: Path,
) -> None:
    raw_path = _strict_report_value(report_path, text, path_field)
    raw_hash = _strict_report_value(report_path, text, hash_field)
    root = project_root.resolve(strict=True)
    expected = expected_path.resolve(strict=True)
    try:
        expected_relative = expected.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Expected {path_field} is outside project root: {expected}") from exc
    normalized = raw_path.replace("\\", "/")
    parsed = PurePosixPath(normalized)
    if (
        parsed.is_absolute()
        or bool(Path(normalized).drive)
        or ".." in parsed.parts
        or parsed.as_posix() != normalized
        or normalized.casefold() != expected_relative.casefold()
    ):
        raise ValueError(
            f"Plugin post-verify {path_field} mismatch: "
            f"expected {expected_relative!r}, found {raw_path!r}"
        )
    if _SHA256.fullmatch(raw_hash) is None:
        raise ValueError(f"Plugin post-verify {hash_field} is invalid: {raw_hash!r}")
    actual_hash = sha256_file(expected)
    if raw_hash.casefold() != actual_hash:
        raise ValueError(
            f"Plugin post-verify {hash_field} mismatch: "
            f"expected {actual_hash}, found {raw_hash}"
        )


def validate_plugin_post_verify_report(
    path: Path,
    *,
    project_root: Path,
    expected_game: str,
    expected_adapter: str,
    expected_original: Path,
    expected_output: Path,
    expected_translation_jsonl: Path,
    expected_output_export_jsonl: Path,
    expected_writeback_report: Path,
    expected_invariant_report: Path,
) -> PluginPostVerifyEvidence:
    text = read_plugin_report_text(path)
    game_id = _strict_report_value(path, text, "game_id")
    adapter_id = _strict_report_value(path, text, "plugin_adapter")
    if game_id != expected_game:
        raise ValueError(
            f"Plugin post-verify game_id mismatch: expected {expected_game!r}, found {game_id!r}"
        )
    if adapter_id != expected_adapter:
        raise ValueError(
            "Plugin post-verify plugin_adapter mismatch: "
            f"expected {expected_adapter!r}, found {adapter_id!r}"
        )
    for path_field, hash_field, expected in (
        ("Original", "Original SHA256", expected_original),
        ("Output", "Output SHA256", expected_output),
        ("Translation JSONL", "Translation JSONL SHA256", expected_translation_jsonl),
        ("Output export JSONL", "Output export JSONL SHA256", expected_output_export_jsonl),
        ("Writeback report", "Writeback report SHA256", expected_writeback_report),
        ("Invariant report", "Invariant report SHA256", expected_invariant_report),
    ):
        _validate_report_path_and_hash(
            path,
            text,
            project_root=project_root,
            path_field=path_field,
            hash_field=hash_field,
            expected_path=expected,
        )

    true_fields = (
        "Verification passed",
        "Writeback reparse verified",
        "Structural validation verified",
        "Round-trip verified",
    )
    for field in true_fields:
        if _strict_report_value(path, text, field).casefold() != "true":
            raise ValueError(f"Plugin post-verify {field} must be true: {path}")
    blocking = _strict_report_value(path, text, "Blocking issues")
    rows = _strict_report_value(path, text, "Translation rows verified")
    if not blocking.isdigit() or int(blocking) != 0:
        raise ValueError(f"Plugin post-verify Blocking issues must be zero: {path}")
    if not rows.isdigit() or int(rows) <= 0:
        raise ValueError(
            f"Plugin post-verify Translation rows verified must be positive: {path}"
        )
    return PluginPostVerifyEvidence(
        translation_rows_verified=int(rows),
        blocking_issues=int(blocking),
    )


def merge_plugin_report_traits(*reports: PluginReportTraits) -> PluginReportTraits:
    values: dict[str, bool | None] = {}
    for report in reports:
        for field in TRAIT_FIELDS:
            value = getattr(report, field)
            if value is None:
                continue
            if field in values and values[field] is not value:
                raise ValueError(f"Conflicting plugin trait values for {field}")
            values[field] = value
    return PluginReportTraits(**values)


def plugin_resource_descriptor(
    context: GameContext,
    relative_path: Path,
    report_traits: PluginReportTraits | None = None,
) -> ResourceDescriptor:
    traits = report_traits.resource_traits() if report_traits is not None else frozenset()
    return classify_resource(context, relative_path, traits=traits)


def capability_evidence(
    resource: ResourceDescriptor,
    decision: CapabilityDecision,
    *,
    report_traits: PluginReportTraits | None = None,
    supported: bool | None = None,
    error_code: str | None = None,
    reason: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    effective_supported = decision.supported if supported is None else supported
    effective_error = decision.error_code if error_code is None else error_code
    effective_reason = reason or decision.reason
    row: dict[str, Any] = {
        "resource_path": resource.relative_path.as_posix(),
        "resource_category": resource.category,
        "resource_subtype": resource.subtype,
        "resource_container": resource.container,
        "resource_traits": sorted(resource.traits),
        "capability": decision.capability,
        "operation": decision.operation,
        "effective_level": decision.level,
        "strict_complete_allowed": decision.strict_complete_allowed,
        "supported": effective_supported,
        "error_code": None if effective_supported else (effective_error or "capability_unsupported"),
        "reason": effective_reason,
    }
    if report_traits is not None:
        row["adapter_traits"] = report_traits.as_report_values()
    if evidence:
        row["evidence"] = evidence
    return row


def capability_attempt_evidence(
    resource: ResourceDescriptor,
    decision: CapabilityDecision,
    *,
    phase: str,
    evidence_kind: str = "adapter_attempt",
    result: str,
    return_code: int,
    report_path: str,
    report_sha256: str = "",
    error_code: str | None = None,
    reason: str = "",
    report_traits: PluginReportTraits | None = None,
) -> dict[str, Any]:
    if result not in {"success", "failed", "blocked"}:
        raise ValueError(f"Invalid plugin adapter attempt result: {result!r}")
    if evidence_kind not in {"adapter_attempt", "verification_attempt"}:
        raise ValueError(f"Invalid plugin attempt evidence kind: {evidence_kind!r}")
    row = capability_evidence(
        resource,
        decision,
        report_traits=report_traits,
        reason=reason,
        evidence=report_path,
    )
    row.update(
        {
            "evidence_kind": evidence_kind,
            "phase": phase,
            "result": result,
            "return_code": return_code,
            "error_code": error_code,
            "report_path": report_path,
            "report_sha256": report_sha256,
        }
    )
    return row
