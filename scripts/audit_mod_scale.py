"""Estimate Mod translation scale and resource risk without materializing input."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from bethesda_archive_adapter import read_ba2_entries, read_bsa_entries
from capability_resolver import resolve_resource_capability
from file_utils import is_reparse_point, py7zr_available, sha256_file
from game_context import GameContext
from new_ba2_archive_manifest import validate_archive_relative_path
from project_paths import (
    is_under,
    plugin_root,
    project_root,
    relative_path,
    resolve_project_path,
    safe_file_name,
)
from report_utils import utc_now
from resource_model import ResourceDescriptor, classify_resource
from route_translation_task import current_game_context


SCALE_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
CLASSIFICATION_METRICS = (
    "max_unpacked_bytes",
    "max_file_count",
    "max_candidate_rows",
    "max_archive_count",
)
PLUGIN_HEADER_SIZE = 24
TES4_LOCALIZED_FLAG = 0x00000080
TES4_LIGHT_FLAG = 0x00000200
MAX_RECORDED_WARNINGS = 20
ABSOLUTE_LIMIT_KEYS = {
    "max_files",
    "max_file_bytes",
    "max_total_bytes",
    "timeout_seconds",
    "max_parallel_tasks",
    "max_parallel_binary_tasks",
    "max_parallel_archive_tasks",
    "max_translation_batch_rows",
    "max_checkpoint_every_files",
}
EXTRACT_MODES = {"full", "filtered", "selective", "selective-sharded", "multi-project"}
PACKAGE_MODES = {"complete", "translation-overlay", "aggregate-only"}


@dataclass
class AssessmentAccumulator:
    file_count: int = 0
    estimated_unpacked_bytes: int = 0
    largest_file_bytes: int = 0
    archive_count: int = 0
    plugin_count: int = 0
    pex_count: int = 0
    string_table_count: int = 0
    candidate_file_count: int = 0
    estimated_candidate_rows: int = 0
    protected_bytes: int = 0
    mcm_file_count: int = 0
    fomod_file_count: int = 0
    localized_plugin_count: int = 0
    light_plugin_count: int = 0
    experimental_write_resource_count: int = 0
    unknown_format_count: int = 0
    unsafe_path_count: int = 0
    encrypted_entry_count: int = 0
    manual_archive_count: int = 0
    opaque_archive_count: int = 0
    controlled_archive_count: int = 0
    archive_container_bytes: int = 0
    inventory_complete: bool = True
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_RECORDED_WARNINGS:
            self.warnings.append(message)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def load_scale_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Mod scale profile config must use schema_version=1")

    estimates = payload.get("candidate_row_estimates")
    if not isinstance(estimates, dict):
        raise ValueError("candidate_row_estimates must be an object")
    _positive_int(estimates.get("default_bytes_per_row"), "default_bytes_per_row")
    for mapping_name in ("category_bytes_per_row", "subtype_bytes_per_row"):
        values = estimates.get(mapping_name)
        if not isinstance(values, dict) or not values:
            raise ValueError(f"{mapping_name} must be a non-empty object")
        for key, value in values.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{mapping_name} keys must be non-empty strings")
            _positive_int(value, f"{mapping_name}.{key}")

    absolute_limits = payload.get("absolute_limits")
    if not isinstance(absolute_limits, dict) or set(absolute_limits) != ABSOLUTE_LIMIT_KEYS:
        raise ValueError("absolute_limits has an invalid key set")
    for key, value in absolute_limits.items():
        _positive_int(value, f"absolute_limits.{key}")
    disk_policy = payload.get("disk_policy")
    if not isinstance(disk_policy, dict):
        raise ValueError("disk_policy must be an object")
    _positive_int(disk_policy.get("safety_margin_bytes"), "disk_policy.safety_margin_bytes")
    for key in ("new_materialization_multiplier", "refresh_materialization_multiplier"):
        value = disk_policy.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"disk_policy.{key} must be a positive number")
    materialization = payload.get("materialization_policy")
    if not isinstance(materialization, dict):
        raise ValueError("materialization_policy must be an object")
    for key in ("selectable_categories", "excluded_containers"):
        values = materialization.get(key)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"materialization_policy.{key} must be a non-empty string list")

    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(SCALE_LEVELS):
        raise ValueError("profiles must define L0 through L5 entries")
    previous_limits = {metric: -1 for metric in CLASSIFICATION_METRICS}
    for level in SCALE_LEVELS:
        profile = profiles.get(level)
        if not isinstance(profile, dict):
            raise ValueError(f"profiles.{level} must be an object")
        if not isinstance(profile.get("display_name"), str) or not profile["display_name"].strip():
            raise ValueError(f"profiles.{level}.display_name must be non-empty")
        classification = profile.get("classification")
        recommendations = profile.get("recommendations")
        if not isinstance(classification, dict) or not isinstance(recommendations, dict):
            raise ValueError(f"profiles.{level} must define classification and recommendations")
        if level == "L5":
            if classification:
                raise ValueError("profiles.L5.classification must be empty")
            if recommendations.get("extract_mode") != "multi-project" or recommendations.get("package_mode") != "aggregate-only":
                raise ValueError("profiles.L5 must use multi-project and aggregate-only")
            continue
        if set(classification) != set(CLASSIFICATION_METRICS):
            raise ValueError(f"profiles.{level}.classification has an invalid metric set")
        for metric in CLASSIFICATION_METRICS:
            limit = _positive_int(classification.get(metric), f"profiles.{level}.{metric}")
            if limit <= previous_limits[metric]:
                raise ValueError(f"Scale threshold {metric} must increase from L0 through L4")
            previous_limits[metric] = limit
        extract_mode = recommendations.get("extract_mode")
        package_mode = recommendations.get("package_mode")
        if extract_mode not in EXTRACT_MODES or package_mode not in PACKAGE_MODES:
            raise ValueError(f"profiles.{level} has an invalid extract/package mode")
        for key, value in recommendations.items():
            if key in {"extract_mode", "package_mode"}:
                continue
            _positive_int(value, f"profiles.{level}.recommendations.{key}")
            if key in absolute_limits and value > absolute_limits[key]:
                raise ValueError(f"profiles.{level}.{key} exceeds the absolute safety cap")
        bounded_recommendations = {
            "translation_batch_rows": "max_translation_batch_rows",
            "checkpoint_every_files": "max_checkpoint_every_files",
        }
        for key, cap_key in bounded_recommendations.items():
            if key in recommendations and recommendations[key] > absolute_limits[cap_key]:
                raise ValueError(f"profiles.{level}.{key} exceeds {cap_key}")

    risks = payload.get("risk_profiles")
    if not isinstance(risks, dict) or set(risks) != set(RISK_LEVELS):
        raise ValueError("risk_profiles must define R0 through R4 entries")
    if not all(isinstance(value, str) and value.strip() for value in risks.values()):
        raise ValueError("risk_profiles values must be non-empty strings")
    return payload


def default_scale_config_path() -> Path:
    return plugin_root() / "config" / "mod_scale_profiles.json"


def resolve_scale_config_path(root: Path, value: str) -> Path:
    if not value.strip():
        return default_scale_config_path().resolve(strict=True)
    return resolve_project_path(root, value, must_exist=True)


def config_evidence_path(root: Path, config_path: Path) -> str:
    resolved = config_path.resolve(strict=True)
    plugin_base = plugin_root().resolve(strict=True)
    if is_under(resolved, plugin_base):
        return "plugin:" + relative_path(plugin_base, resolved).replace("\\", "/")
    if is_under(resolved, root):
        return "workspace:" + relative_path(root, resolved).replace("\\", "/")
    return f"external:{resolved.name}"


def plugin_header_traits(header: bytes) -> frozenset[str]:
    if len(header) < PLUGIN_HEADER_SIZE or header[:4] != b"TES4":
        return frozenset()
    flags = int.from_bytes(header[8:12], byteorder="little", signed=False)
    traits: set[str] = set()
    if flags & TES4_LOCALIZED_FLAG:
        traits.add("localized")
    if flags & TES4_LIGHT_FLAG:
        traits.add("light")
    return frozenset(traits)


def _candidate_bytes_per_row(
    descriptor: ResourceDescriptor,
    config: Mapping[str, Any],
) -> int:
    estimates = config["candidate_row_estimates"]
    subtype_values = estimates["subtype_bytes_per_row"]
    category_values = estimates["category_bytes_per_row"]
    return int(
        subtype_values.get(
            descriptor.subtype,
            category_values.get(
                descriptor.category,
                estimates["default_bytes_per_row"],
            ),
        )
    )


def _is_candidate(descriptor: ResourceDescriptor) -> bool:
    if descriptor.container == "protected" or descriptor.category == "protected_binary":
        return False
    return descriptor.category in {"loose_text", "papyrus", "plugin", "string_table"}


def observe_entry(
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
    relative_path_value: Path,
    size: int,
    *,
    header: bytes = b"",
) -> None:
    traits = plugin_header_traits(header)
    descriptor = classify_resource(context, relative_path_value, traits=traits)
    accumulator.file_count += 1
    accumulator.estimated_unpacked_bytes += max(0, size)
    accumulator.largest_file_bytes = max(accumulator.largest_file_bytes, max(0, size))

    if descriptor.category in {"archive", "package"}:
        accumulator.archive_count += 1
        accumulator.opaque_archive_count += 1
        accumulator.inventory_complete = False
        accumulator.warn(
            "Nested or dedicated archive contents were not inspected in this pass: "
            f"{relative_path_value.as_posix()}"
        )
        if relative_path_value.suffix.casefold() == ".rar":
            accumulator.manual_archive_count += 1
    if descriptor.category == "plugin":
        accumulator.plugin_count += 1
        if "localized" in descriptor.traits:
            accumulator.localized_plugin_count += 1
        if "light" in descriptor.traits:
            accumulator.light_plugin_count += 1
    if descriptor.capability and descriptor.category in {"papyrus", "plugin"}:
        write_decision = resolve_resource_capability(context, descriptor, "write")
        if write_decision.level == "experimental_write":
            accumulator.experimental_write_resource_count += 1
    if descriptor.subtype == "papyrus.binary":
        accumulator.pex_count += 1
    if descriptor.category == "string_table":
        accumulator.string_table_count += 1
    if descriptor.container == "mcm":
        accumulator.mcm_file_count += 1
    if any(part.casefold() == "fomod" for part in relative_path_value.parts[:-1]):
        accumulator.fomod_file_count += 1
    if descriptor.container == "protected" or descriptor.category == "protected_binary":
        accumulator.protected_bytes += max(0, size)
    if descriptor.category == "unknown" and descriptor.container != "protected":
        accumulator.unknown_format_count += 1

    if _is_candidate(descriptor):
        accumulator.candidate_file_count += 1
        if size > 0:
            accumulator.estimated_candidate_rows += max(
                1,
                math.ceil(size / _candidate_bytes_per_row(descriptor, config)),
            )


def _read_header(path: Path, extension: str) -> bytes:
    if extension not in {".esp", ".esm", ".esl"}:
        return b""
    with path.open("rb") as handle:
        return handle.read(PLUGIN_HEADER_SIZE)


def inventory_bethesda_archive(
    source: Path,
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
) -> bool:
    accumulator.archive_count += 1
    accumulator.archive_container_bytes += source.stat().st_size
    try:
        if source.suffix.casefold() == ".bsa":
            entries = read_bsa_entries(source)
        else:
            _archive_type, entries = read_ba2_entries(source)
    except (OSError, UnicodeError, ValueError) as exc:
        accumulator.file_count += 1
        accumulator.estimated_unpacked_bytes += source.stat().st_size
        accumulator.largest_file_bytes = max(
            accumulator.largest_file_bytes,
            source.stat().st_size,
        )
        accumulator.opaque_archive_count += 1
        accumulator.inventory_complete = False
        accumulator.warn(f"Bethesda archive inventory failed for {source.name}: {exc}")
        return False

    candidate_count_before = accumulator.candidate_file_count
    for entry in entries:
        observe_entry(
            accumulator,
            context,
            config,
            Path(*entry.path.split("/")),
            entry.size,
        )
    if accumulator.candidate_file_count > candidate_count_before:
        accumulator.controlled_archive_count += 1
    return True


def inventory_directory(
    source: Path,
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
) -> str:
    stack = [(source, Path())]
    while stack:
        current, relative_dir = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
        except OSError as exc:
            accumulator.inventory_complete = False
            accumulator.warn(f"Directory inventory failed at {relative_dir or Path('.')}: {exc}")
            continue
        for entry in entries:
            relative = relative_dir / entry.name
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                accumulator.inventory_complete = False
                accumulator.warn(f"Entry metadata could not be read: {relative}: {exc}")
                continue
            if entry.is_symlink() or is_reparse_point(entry_stat):
                accumulator.inventory_complete = False
                accumulator.unsafe_path_count += 1
                accumulator.warn(f"Link or reparse-point entry skipped: {relative}")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append((Path(entry.path), relative))
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            path = Path(entry.path)
            if path.suffix.casefold() in {".bsa", ".ba2"}:
                inventory_bethesda_archive(path, accumulator, context, config)
                continue
            try:
                header = _read_header(path, path.suffix.casefold())
            except OSError as exc:
                header = b""
                accumulator.inventory_complete = False
                accumulator.warn(f"Plugin header could not be read: {relative}: {exc}")
            observe_entry(
                accumulator,
                context,
                config,
                relative,
                entry_stat.st_size,
                header=header,
            )
    return "source-tree-metadata"


def _validated_archive_path(
    raw_name: str,
    accumulator: AssessmentAccumulator,
) -> Path | None:
    try:
        normalized = validate_archive_relative_path(raw_name)
    except ValueError as exc:
        accumulator.inventory_complete = False
        accumulator.unsafe_path_count += 1
        accumulator.warn(str(exc))
        return None
    return Path(*normalized.split("/"))


def inventory_zip(
    source: Path,
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
) -> str:
    accumulator.archive_count += 1
    with zipfile.ZipFile(source, "r") as archive:
        for member in archive.infolist():
            if member.is_dir() or member.filename.endswith(("/", "\\")):
                continue
            relative = _validated_archive_path(member.filename, accumulator)
            if relative is None:
                continue
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if unix_mode and stat.S_ISLNK(unix_mode):
                accumulator.inventory_complete = False
                accumulator.unsafe_path_count += 1
                accumulator.warn(f"ZIP link entry skipped: {relative.as_posix()}")
                continue
            if member.flag_bits & 0x1:
                accumulator.encrypted_entry_count += 1
                accumulator.inventory_complete = False
                accumulator.warn(f"Encrypted ZIP entry cannot be inspected: {relative.as_posix()}")
            header = b""
            if relative.suffix.casefold() in {".esp", ".esm", ".esl"} and not (member.flag_bits & 0x1):
                try:
                    with archive.open(member, "r") as handle:
                        header = handle.read(PLUGIN_HEADER_SIZE)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    accumulator.inventory_complete = False
                    accumulator.warn(f"Plugin header could not be read: {relative.as_posix()}: {exc}")
            observe_entry(
                accumulator,
                context,
                config,
                relative,
                member.file_size,
                header=header,
            )
    return "zip-central-directory"


def inventory_7z(
    source: Path,
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
) -> str:
    accumulator.archive_count += 1
    if not py7zr_available():
        accumulator.inventory_complete = False
        accumulator.warn("py7zr is unavailable; 7z contents were not inventoried")
        return "source-file-metadata"

    import py7zr

    with py7zr.SevenZipFile(source, mode="r") as archive:
        if archive.needs_password():
            accumulator.encrypted_entry_count += 1
            accumulator.inventory_complete = False
            accumulator.warn("Password-protected 7z contents cannot be fully inspected")
        for member in archive.list():
            if bool(getattr(member, "is_directory", False)):
                continue
            relative = _validated_archive_path(str(member.filename), accumulator)
            if relative is None:
                continue
            if bool(getattr(member, "is_symlink", False)) or not bool(
                getattr(member, "is_file", True)
            ):
                accumulator.inventory_complete = False
                accumulator.unsafe_path_count += 1
                accumulator.warn(f"7z link or special entry skipped: {relative.as_posix()}")
                continue
            size = int(getattr(member, "uncompressed", 0) or 0)
            observe_entry(accumulator, context, config, relative, size)
            if relative.suffix.casefold() in {".esp", ".esm", ".esl"}:
                accumulator.inventory_complete = False
                accumulator.warn(
                    "7z central-directory inventory cannot inspect plugin header traits: "
                    f"{relative.as_posix()}"
                )
    return "7z-central-directory"


def inventory_source(
    source: Path,
    accumulator: AssessmentAccumulator,
    context: GameContext,
    config: Mapping[str, Any],
) -> tuple[str, str, int]:
    if source.is_dir():
        return "directory", inventory_directory(source, accumulator, context, config), 0

    compressed_bytes = source.stat().st_size
    extension = source.suffix.casefold()
    if extension == ".zip":
        return "zip", inventory_zip(source, accumulator, context, config), compressed_bytes
    if extension == ".7z":
        return "7z", inventory_7z(source, accumulator, context, config), compressed_bytes
    if extension in {".bsa", ".ba2"}:
        complete = inventory_bethesda_archive(source, accumulator, context, config)
        basis = "bethesda-archive-metadata" if complete else "source-file-metadata"
        return extension.removeprefix("."), basis, 0

    header = _read_header(source, extension)
    observe_entry(accumulator, context, config, Path(source.name), compressed_bytes, header=header)
    return "file", "source-file-metadata", compressed_bytes


def classify_scale(metrics: Mapping[str, int], config: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    profiles = config["profiles"]
    metric_levels: dict[str, str] = {}
    for metric, value in metrics.items():
        selected = "L5"
        for level in SCALE_LEVELS[:-1]:
            if value <= int(profiles[level]["classification"][metric]):
                selected = level
                break
        metric_levels[metric] = selected
    scale_level = max(metric_levels.values(), key=SCALE_LEVELS.index)
    return scale_level, metric_levels


def classify_risk(accumulator: AssessmentAccumulator) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = "R0"

    def raise_to(candidate: str, reason: str) -> None:
        nonlocal level
        if RISK_LEVELS.index(candidate) > RISK_LEVELS.index(level):
            level = candidate
        reasons.append(reason)

    if accumulator.plugin_count:
        raise_to("R1", f"plugin resources: {accumulator.plugin_count}")
    if accumulator.controlled_archive_count:
        raise_to(
            "R1",
            f"archives containing translation candidates: {accumulator.controlled_archive_count}",
        )
    archive_resources = max(0, accumulator.opaque_archive_count)
    if archive_resources:
        raise_to("R1", f"nested or dedicated archive resources: {archive_resources}")
    if accumulator.pex_count:
        raise_to("R2", f"PEX resources: {accumulator.pex_count}")
    if accumulator.plugin_count > 1:
        raise_to("R2", f"multiple plugins: {accumulator.plugin_count}")
    if accumulator.mcm_file_count:
        raise_to("R2", f"MCM resources: {accumulator.mcm_file_count}")
    if accumulator.fomod_file_count:
        raise_to("R2", f"FOMOD resources: {accumulator.fomod_file_count}")
    if accumulator.string_table_count:
        raise_to("R3", f"STRINGS-family resources: {accumulator.string_table_count}")
    if accumulator.localized_plugin_count:
        raise_to("R3", f"localized plugins: {accumulator.localized_plugin_count}")
    if accumulator.light_plugin_count:
        raise_to("R3", f"light plugins: {accumulator.light_plugin_count}")
    if accumulator.experimental_write_resource_count:
        raise_to(
            "R3",
            f"experimental write resources: {accumulator.experimental_write_resource_count}",
        )
    if accumulator.unknown_format_count:
        raise_to("R4", f"unknown formats: {accumulator.unknown_format_count}")
    if accumulator.unsafe_path_count:
        raise_to("R4", f"unsafe paths: {accumulator.unsafe_path_count}")
    if accumulator.encrypted_entry_count:
        raise_to("R4", f"encrypted entries: {accumulator.encrypted_entry_count}")
    if accumulator.manual_archive_count:
        raise_to("R4", f"manual archive resources: {accumulator.manual_archive_count}")
    return level, reasons or ["only low-risk resources were detected"]


def assess_source(
    root: Path,
    source: Path,
    mod_name: str,
    context: GameContext,
    config_path: Path,
) -> dict[str, Any]:
    config = load_scale_config(config_path)
    accumulator = AssessmentAccumulator()
    source_type, estimation_basis, compressed_bytes = inventory_source(
        source,
        accumulator,
        context,
        config,
    )
    compressed_bytes += accumulator.archive_container_bytes
    scale_metrics = {
        "max_unpacked_bytes": accumulator.estimated_unpacked_bytes,
        "max_file_count": accumulator.file_count,
        "max_candidate_rows": accumulator.estimated_candidate_rows,
        "max_archive_count": accumulator.archive_count,
    }
    scale_level, metric_levels = classify_scale(scale_metrics, config)
    risk_level, risk_reasons = classify_risk(accumulator)
    scale_profile = config["profiles"][scale_level]
    risk_name = config["risk_profiles"][risk_level]

    return {
        "schema_version": 1,
        "report_type": "mod-scale-assessment",
        "generated_at": utc_now(),
        "mod_name": mod_name,
        "game_id": context.game_id,
        "source_path": relative_path(root, source).replace("\\", "/"),
        "source_type": source_type,
        "estimation_basis": estimation_basis,
        "inventory_complete": accumulator.inventory_complete,
        "compressed_bytes": compressed_bytes,
        "estimated_unpacked_bytes": accumulator.estimated_unpacked_bytes,
        "largest_file_bytes": accumulator.largest_file_bytes,
        "file_count": accumulator.file_count,
        "archive_count": accumulator.archive_count,
        "opaque_archive_count": accumulator.opaque_archive_count,
        "controlled_archive_count": accumulator.controlled_archive_count,
        "plugin_count": accumulator.plugin_count,
        "pex_count": accumulator.pex_count,
        "string_table_count": accumulator.string_table_count,
        "localized_plugin_count": accumulator.localized_plugin_count,
        "light_plugin_count": accumulator.light_plugin_count,
        "experimental_write_resource_count": accumulator.experimental_write_resource_count,
        "mcm_file_count": accumulator.mcm_file_count,
        "fomod_file_count": accumulator.fomod_file_count,
        "candidate_file_count": accumulator.candidate_file_count,
        "estimated_candidate_rows": accumulator.estimated_candidate_rows,
        "candidate_rows_are_estimated": True,
        "protected_bytes": accumulator.protected_bytes,
        "unknown_format_count": accumulator.unknown_format_count,
        "unsafe_path_count": accumulator.unsafe_path_count,
        "encrypted_entry_count": accumulator.encrypted_entry_count,
        "manual_archive_count": accumulator.manual_archive_count,
        "scale_level": scale_level,
        "scale_name": scale_profile["display_name"],
        "metric_levels": {
            "unpacked_bytes": metric_levels["max_unpacked_bytes"],
            "file_count": metric_levels["max_file_count"],
            "candidate_rows": metric_levels["max_candidate_rows"],
            "archive_count": metric_levels["max_archive_count"],
        },
        "risk_level": risk_level,
        "risk_name": risk_name,
        "risk_reasons": risk_reasons,
        "recommended_profile": f"{scale_profile['display_name']}-{risk_name}",
        "recommended_settings": dict(scale_profile["recommendations"]),
        "recommendations_status": "advisory-not-enforced",
        "execution_behavior_changed": False,
        "config_path": config_evidence_path(root, config_path),
        "config_sha256": sha256_file(config_path),
        "warnings": accumulator.warnings,
    }


def write_scale_assessment(report_path: Path, payload: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Estimate Bethesda Mod scale and risk without extracting the source.",
    )
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--config-path", default="")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    root = project_root()
    mod_root = resolve_project_path(root, "mod", must_exist=True)
    source = resolve_project_path(root, args.source_path, must_exist=True)
    if not is_under(source, mod_root):
        raise ValueError("SourcePath must stay under the workspace mod/ directory")
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty after sanitization")
    config_path = resolve_scale_config_path(root, args.config_path)
    report_value = args.report_path or f"qa/{mod_name}.scale_assessment.json"
    report_path = resolve_project_path(root, report_value, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError("ReportPath must stay under qa/")

    payload = assess_source(
        root,
        source,
        mod_name,
        current_game_context(root),
        config_path,
    )
    write_scale_assessment(report_path, payload)
    print(f"Scale assessment written to: {report_path}")
    print(f"Classification: {payload['scale_level']}-{payload['risk_level']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
