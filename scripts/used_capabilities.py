"""Summarize capabilities that actually participate in one final_mod delivery."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from adapter_contract import AdapterResult
from adapter_result_io import adapter_result_from_payload, require_translation_input_lane
from adapter_registry import require_adapter
from capability_resolver import CapabilityDecision, resolve_capability, resolve_resource_capability
from file_utils import is_reparse_point, sha256_file, validate_regular_path_under
from game_context import GameContext, load_game_context
from new_ba2_archive_manifest import validate_archive_relative_path
from plugin_resource_evidence import (
    PluginReportTraits,
    capability_evidence,
    read_plugin_report_traits,
    unknown_write_plugin_trait_fields,
    validate_plugin_report_status,
)
from verify_ba2_extraction import verify_manifest as verify_ba2_manifest
from project_paths import (
    final_mod_dir as canonical_final_mod_dir,
    project_root,
    relative_posix_path,
    require_under_any,
    resolve_project_path,
    safe_file_name,
)
from resource_model import ResourceDescriptor, classify_resource


SCHEMA_VERSION = 1
MAX_PROVENANCE_BYTES = 32 * 1024 * 1024
MAX_PROVENANCE_LINE_CHARS = 1024 * 1024
MAX_PROVENANCE_ROWS = 100_000
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
PLUGIN_EXTENSIONS = frozenset({".esp", ".esm", ".esl"})
LOOSE_TEXT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".ini",
        ".json",
        ".jsonl",
        ".md",
        ".psc",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_GAME_LINE = re.compile(r"(?mi)^-\s*game_id:\s*(\S+)\s*$")
_CAPABILITY_LINE = re.compile(
    r"(?mi)^-\s*(?:plugin_text_)?capability_level:\s*(\S+)\s*$"
)
_FAILURE_STATUS_LINE = re.compile(r"(?mi)^-\s*status:\s*(?:failed|error|blocked)\s*$")
_JSON_ARTIFACT_PATH = re.compile(
    r'"path"\s*:\s*("(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)
_REPORT_OUTPUT_LINE = re.compile(r"(?mi)^-\s*Output (?:plugin|PEX):\s*(.+?)\s*$")


class UsedCapabilityError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _fail(error_code: str, message: str) -> None:
    raise UsedCapabilityError(error_code, message)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _claim_key(root: Path, value: str, *, label: str) -> str:
    candidate = Path(value.replace("\\", os.sep).replace("/", os.sep))
    if candidate.is_absolute() or not candidate.parts:
        _fail("adapter_failed", f"{label} must be a workspace-relative path: {value!r}")
    lexical_root = _absolute_lexical(root)
    lexical_path = _absolute_lexical(lexical_root / candidate)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        _fail("adapter_failed", f"{label} escapes the workspace: {value!r}")
    return relative.as_posix().casefold()


def _receipt_text_claims_path(root: Path, text: str, expected_key: str) -> bool:
    for token in _JSON_ARTIFACT_PATH.findall(text):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, str):
            continue
        try:
            if _claim_key(root, value, label="AdapterResult string") == expected_key:
                return True
        except UsedCapabilityError:
            continue
    return False


def _validate_no_reparse_chain(path: Path, root: Path, *, include_leaf: bool = True) -> None:
    lexical_root = _absolute_lexical(root)
    lexical_path = _absolute_lexical(path)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        _fail("verification_failed", f"Path is outside the workspace: {path}")
    current = lexical_root
    candidates = [lexical_root]
    for part in relative.parts:
        current = current / part
        candidates.append(current)
    if not include_leaf and candidates:
        candidates = candidates[:-1]
    for candidate in candidates:
        if not os.path.lexists(candidate):
            continue
        try:
            entry_stat = candidate.lstat()
        except OSError as exc:
            _fail("verification_failed", f"Unable to inspect path component {candidate}: {exc}")
        if candidate.is_symlink() or is_reparse_point(entry_stat):
            _fail("verification_failed", f"Path component is a symlink or reparse point: {candidate}")
        if candidate != lexical_path and not stat.S_ISDIR(entry_stat.st_mode):
            _fail("verification_failed", f"Path parent is not a regular directory: {candidate}")


def _read_bounded_text(path: Path, *, label: str, max_bytes: int) -> str:
    try:
        if path.stat().st_size > max_bytes:
            _fail("verification_failed", f"{label} exceeds {max_bytes} bytes: {path}")
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        _fail("verification_failed", f"{label} is not valid UTF text: {path}: {exc}")
    except OSError as exc:
        _fail("verification_failed", f"Unable to read {label}: {path}: {exc}")


def _read_json_object(
    path: Path,
    *,
    label: str,
    text: str | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(
            text if text is not None else _read_bounded_text(path, label=label, max_bytes=MAX_RECEIPT_BYTES)
        )
    except json.JSONDecodeError as exc:
        _fail("verification_failed", f"Invalid {label}: {path}: {exc}")
    if not isinstance(payload, dict):
        _fail("verification_failed", f"{label} must contain a JSON object: {path}")
    return payload


def _relative_final_file(final_mod: Path, value: object) -> Path:
    text = str(value or "").replace("\\", "/")
    prefix = "final_mod/"
    if not text.casefold().startswith(prefix):
        _fail("verification_failed", f"Invalid final_mod provenance file path: {text!r}")
    relative = Path(text[len(prefix) :])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        _fail("verification_failed", f"Unsafe final_mod provenance file path: {text!r}")
    path = final_mod / relative
    try:
        validate_regular_path_under(path, final_mod, kind="file", label="Final provenance file")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    return path


def _read_provenance(
    root: Path,
    final_mod: Path,
    context: GameContext,
) -> tuple[Path, list[dict[str, Any]]]:
    path = final_mod / "meta" / "provenance.jsonl"
    try:
        _validate_no_reparse_chain(path, root)
        validate_regular_path_under(path, final_mod, kind="file", label="Provenance ledger")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", f"Missing or unsafe final_mod provenance: {exc}")
    if path.stat().st_size > MAX_PROVENANCE_BYTES:
        _fail("verification_failed", f"Provenance ledger exceeds {MAX_PROVENANCE_BYTES} bytes")
    rows: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number > MAX_PROVENANCE_ROWS:
                _fail("verification_failed", "Provenance ledger has too many rows")
            if len(line) > MAX_PROVENANCE_LINE_CHARS:
                _fail("verification_failed", f"Provenance line {line_number} is too long")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                _fail("verification_failed", f"Invalid provenance JSONL line {line_number}: {exc}")
            if not isinstance(row, dict):
                _fail("verification_failed", f"Provenance line {line_number} must be an object")
            if str(row.get("game_id", "")) != context.game_id:
                _fail(
                    "profile_error",
                    f"Provenance game_id does not match workspace profile on line {line_number}",
                )
            if str(row.get("status", "")) != "assembled":
                continue
            file_key = str(row.get("file", "")).replace("\\", "/").casefold()
            if file_key in seen_files:
                _fail("verification_failed", f"Duplicate assembled provenance file: {file_key}")
            seen_files.add(file_key)
            final_file = _relative_final_file(final_mod, row.get("file"))
            expected_final_hash = str(row.get("file_sha256", "")).casefold()
            if not expected_final_hash or sha256_file(final_file) != expected_final_hash:
                _fail("verification_failed", f"Final provenance hash mismatch: {row.get('file', '')}")
            rows.append(row)
    return path, rows


def _row_capabilities(row: dict[str, Any]) -> list[tuple[str, str]]:
    transform = str(row.get("transform", "")).strip()
    extension = Path(str(row.get("file", ""))).suffix.casefold()
    if transform == "controlled-tool-output":
        if extension in PLUGIN_EXTENSIONS:
            return [("plugin_text", "write")]
        if extension == ".pex":
            return [("pex", "write")]
        _fail(
            "verification_failed",
            f"Unknown controlled tool output capability for {row.get('file', '')}",
        )
    if transform == "text-resource-translation":
        if extension not in LOOSE_TEXT_EXTENSIONS:
            _fail(
                "verification_failed",
                f"Unsupported loose-text extension for text-resource-translation: "
                f"{row.get('file', '')}",
            )
        return [("loose_text", "write")]
    if transform == "ba2-loose-override":
        if extension not in LOOSE_TEXT_EXTENSIONS:
            _fail(
                "verification_failed",
                f"Unsupported BA2 loose-override extension: {row.get('file', '')}",
            )
        return [("archive.ba2", "read"), ("loose_text", "write")]
    if transform == "bsa-loose-override":
        if extension not in LOOSE_TEXT_EXTENSIONS:
            _fail(
                "verification_failed",
                f"Unsupported BSA loose-override extension: {row.get('file', '')}",
            )
        return [("archive.bsa", "read"), ("loose_text", "write")]
    if transform == "original-copy":
        return []
    _fail(
        "verification_failed",
        f"Unknown assembled provenance transform {transform!r}: {row.get('file', '')}",
    )


def _decision(
    context: GameContext,
    capability: str,
    operation: str,
    resource: ResourceDescriptor | None = None,
) -> CapabilityDecision:
    try:
        decision = (
            resolve_resource_capability(context, resource, operation)
            if resource is not None
            else resolve_capability(context, capability, operation)
        )
    except ValueError as exc:
        _fail("profile_error", str(exc))
    if not decision.supported or not decision.adapter_id:
        _fail(
            decision.error_code or "capability_unsupported",
            decision.reason,
        )
    try:
        adapter_operation = {"inventory": "inventory", "read": "extract", "write": "apply"}[operation]
        require_adapter(decision.adapter_id, adapter_operation)
    except ValueError as exc:
        _fail("adapter_missing", str(exc))
    return decision


def _resource_relative_path(
    row: dict[str, Any],
    capability: str,
    final_mod: Path,
) -> Path:
    if capability in {"archive.ba2", "archive.bsa"}:
        archive_path = str(row.get("archive_path", "")).replace("\\", "/").strip()
        if not archive_path:
            _fail(
                "verification_failed",
                f"Archive loose override is missing archive evidence (archive_path): "
                f"{row.get('file', '')}",
            )
        parts = Path(archive_path).parts
        if parts and parts[0].casefold() == "mod":
            parts = parts[1:]
        if not parts:
            _fail("verification_failed", f"Archive capability has invalid archive_path: {archive_path}")
        return Path(*parts)
    final_file = _relative_final_file(final_mod, row.get("file"))
    return final_file.relative_to(final_mod)


def _plugin_traits_from_evidence(root: Path, evidence: list[str]) -> PluginReportTraits:
    values: dict[str, bool | None] = {}
    unknown_fields: set[str] = set()
    for relative in evidence:
        path = root / Path(relative)
        if path.suffix.casefold() != ".md" or not path.is_file():
            continue
        try:
            traits = read_plugin_report_traits(path)
        except (OSError, ValueError) as exc:
            _fail("verification_failed", f"Invalid plugin trait evidence {relative}: {exc}")
        for field in (
            "localized",
            "light_by_extension",
            "light_by_header",
            "contains_unsupported_light_formids",
        ):
            value = getattr(traits, field)
            if value is None:
                unknown_fields.add(field)
                continue
            if field in values and values[field] is not value:
                _fail("verification_failed", f"Conflicting plugin trait evidence for {field}")
            values[field] = value
    for field in unknown_fields:
        values[field] = None
    return PluginReportTraits(**values)


def _source_artifact(
    root: Path,
    row: dict[str, Any],
    *,
    capability: str,
    mod_name: str,
    final_mod: Path,
) -> tuple[str, Path, str]:
    source = str(row.get("source", "")).replace("\\", "/")
    if not source or source.startswith("generated:"):
        _fail("verification_failed", f"Delivered tool output has no project source: {source!r}")
    try:
        lexical_source = _absolute_lexical(root / Path(source))
        _validate_no_reparse_chain(lexical_source, root)
        source_path = resolve_project_path(root, lexical_source, must_exist=True)
        validate_regular_path_under(source_path, root, kind="file", label="Delivered source artifact")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    if source_path == final_mod or final_mod in source_path.parents:
        _fail("verification_failed", "Delivered source cannot be inside final_mod")
    if capability == "loose_text":
        allowed_roots = (
            root / "translated" / "final_mod" / mod_name,
            root / "translated" / "overlay" / mod_name,
            root / "out" / mod_name / "final_mod_overlay",
            root / "out" / mod_name / "xtranslator_import",
            root / "out" / mod_name / "dsd_patch",
        )
    else:
        allowed_roots = (
            root / "translated" / "tool_outputs" / mod_name,
            root / "out" / mod_name / "tool_outputs",
        )
    if not any(allowed.resolve(strict=False) in (source_path, *source_path.parents) for allowed in allowed_roots):
        _fail(
            "verification_failed",
            f"Delivered source is outside allowed roots for {capability}: {source}",
        )
    expected = str(row.get("source_sha256", "")).casefold()
    actual = sha256_file(source_path)
    if not expected or actual != expected:
        _fail("verification_failed", f"Delivered source hash mismatch: {source}")
    return source, source_path, actual


def _adapter_result(root: Path, payload: dict[str, Any], path: Path) -> AdapterResult:
    try:
        result = adapter_result_from_payload(payload)
        artifact_keys = [
            _claim_key(root, item.path, label="Adapter artifact path")
            for item in result.artifacts
        ]
        if len(set(artifact_keys)) != len(artifact_keys):
            raise ValueError("artifacts must not contain duplicate normalized paths")
        evidence_files = result.evidence_files
        evidence_keys = [
            _claim_key(root, value, label="Adapter evidence path") for value in evidence_files
        ]
        if len(set(evidence_keys)) != len(evidence_keys):
            raise ValueError("evidence_files must not contain duplicate normalized paths")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        _fail("adapter_failed", f"Invalid AdapterResult {path}: {exc}")


def _validate_receipt_lineage(
    root: Path,
    result: AdapterResult,
    decision: CapabilityDecision,
    mod_name: str,
) -> None:
    if result.mod_name != mod_name:
        _fail(
            "verification_failed",
            f"AdapterResult Mod lane {result.mod_name!r} does not match {mod_name!r}",
        )
    expected_suffix = PLUGIN_EXTENSIONS if decision.capability == "plugin_text" else {".pex"}
    binary_inputs: list[Path] = []
    translation_inputs: list[Path] = []
    for item in result.inputs:
        try:
            path = resolve_project_path(root, item.path, must_exist=True)
            _validate_no_reparse_chain(path, root)
            validate_regular_path_under(path, root, kind="file", label="Adapter input")
        except (OSError, ValueError) as exc:
            _fail("verification_failed", str(exc))
        if sha256_file(path) != item.sha256:
            _fail("verification_failed", f"AdapterResult input hash mismatch: {item.path}")
        if path.suffix.casefold() in expected_suffix:
            binary_inputs.append(path)
        elif path.suffix.casefold() == ".jsonl":
            translation_inputs.append(path)
    if len(binary_inputs) != 1 or len(translation_inputs) != 1 or len(result.inputs) != 2:
        _fail(
            "verification_failed",
            "AdapterResult apply lineage must bind exactly one source binary and one translation JSONL",
        )
    try:
        require_under_any(
            binary_inputs[0],
            [root / "work" / "extracted_mods" / mod_name],
            "Adapter source input",
        )
        require_translation_input_lane(root, translation_inputs[0], mod_name)
    except ValueError as exc:
        _fail("verification_failed", str(exc))


def _receipt_paths(root: Path, mod_name: str) -> list[Path]:
    paths: list[Path] = []
    scan_roots = ((root / "qa", False), (root / "out" / mod_name, True))
    for base, recursive in scan_roots:
        if not base.is_dir():
            continue
        _validate_no_reparse_chain(base, root)
        candidates = base.rglob("*.adapter_result.json") if recursive else base.glob("*.adapter_result.json")
        for path in candidates:
            try:
                _validate_no_reparse_chain(path, root)
                validate_regular_path_under(path, base, kind="file", label="AdapterResult receipt")
            except (OSError, ValueError) as exc:
                _fail("verification_failed", str(exc))
            paths.append(path)
    return sorted(paths, key=lambda item: relative_posix_path(root, item).casefold())


def _validate_evidence(
    root: Path,
    result: AdapterResult,
    context: GameContext,
    decision: CapabilityDecision,
    source_key: str,
) -> list[str]:
    valid_paths: list[str] = []
    artifacts_by_path = {
        _claim_key(root, item.path, label="Adapter artifact path"): item
        for item in result.artifacts
    }
    for value in result.evidence_files:
        try:
            lexical_path = _absolute_lexical(root / Path(value))
            _validate_no_reparse_chain(lexical_path, root)
            path = resolve_project_path(root, value, must_exist=True)
            require_under_any(path, [root / "qa", root / "out"], "Adapter evidence")
            validate_regular_path_under(path, root, kind="file", label="Adapter evidence")
        except (OSError, ValueError) as exc:
            _fail("verification_failed", str(exc))
        evidence_key = _claim_key(root, value, label="Adapter evidence path")
        artifact = artifacts_by_path.get(evidence_key)
        if artifact is None or artifact.sha256 != sha256_file(path):
            _fail("verification_failed", f"Adapter evidence is not hash-bound by receipt: {value}")
        text = _read_bounded_text(path, label="Adapter evidence", max_bytes=MAX_EVIDENCE_BYTES)
        if decision.capability == "plugin_text":
            try:
                validate_plugin_report_status(path, return_code=0)
            except (OSError, ValueError) as exc:
                _fail(
                    "verification_failed",
                    f"Plugin adapter evidence does not have one successful Status=ready: {value}: {exc}",
                )
        games = set(_GAME_LINE.findall(text))
        levels = set(_CAPABILITY_LINE.findall(text))
        report_output_keys: set[str] = set()
        for report_output in _REPORT_OUTPUT_LINE.findall(text):
            try:
                report_output_keys.add(
                    _claim_key(root, report_output.strip().strip("`"), label="Report output path")
                )
            except UsedCapabilityError:
                continue
        report_mentions_source = source_key in report_output_keys
        adapter_marker = (
            f"plugin_adapter: {decision.adapter_id}".casefold() in text.casefold()
            if decision.capability == "plugin_text"
            else "# mutagen pex string tool report" in text.casefold()
        )
        if (
            games == {context.game_id}
            and levels == {decision.level}
            and report_mentions_source
            and adapter_marker
            and (
                decision.capability == "plugin_text"
                or _FAILURE_STATUS_LINE.search(text) is None
            )
        ):
            valid_paths.append(relative_posix_path(root, path))
    if not valid_paths:
        _fail(
            "verification_failed",
            f"Adapter evidence does not prove game_id={context.game_id} "
            f"and capability_level={decision.level}",
        )
    return sorted(set(valid_paths), key=str.casefold)


def _bound_receipt_evidence(
    root: Path,
    row: dict[str, Any],
    context: GameContext,
    decision: CapabilityDecision,
    mod_name: str,
    final_mod: Path,
    receipt_paths: list[Path],
    receipt_text_cache: dict[Path, str],
    receipt_result_cache: dict[Path, AdapterResult],
) -> list[str]:
    source, _source_path, source_hash = _source_artifact(
        root,
        row,
        capability=decision.capability,
        mod_name=mod_name,
        final_mod=final_mod,
    )
    if str(row.get("file_sha256", "")).casefold() != source_hash:
        _fail(
            "verification_failed",
            f"Delivered final file hash does not match its controlled source: "
            f"{row.get('file', '')}",
        )
    source_key = _claim_key(root, source, label="Delivered source path")
    matching: list[tuple[Path, AdapterResult]] = []
    artifact_claim_seen = False
    for receipt_path in receipt_paths:
        receipt_text = receipt_text_cache.get(receipt_path)
        if receipt_text is None:
            receipt_text = _read_bounded_text(
                receipt_path,
                label="AdapterResult",
                max_bytes=MAX_RECEIPT_BYTES,
            )
            receipt_text_cache[receipt_path] = receipt_text
        if not _receipt_text_claims_path(root, receipt_text, source_key):
            continue
        payload = _read_json_object(receipt_path, label="AdapterResult", text=receipt_text)
        artifacts_raw = payload.get("artifacts", [])
        if isinstance(artifacts_raw, list) and any(
            isinstance(item, dict)
            and _claim_key(root, str(item.get("path", "")), label="Adapter artifact path")
            == source_key
            for item in artifacts_raw
        ):
            artifact_claim_seen = True
            result = receipt_result_cache.get(receipt_path)
            if result is None:
                result = _adapter_result(root, payload, receipt_path)
                receipt_result_cache[receipt_path] = result
            matching.append((receipt_path, result))
    if not matching:
        code = "adapter_failed" if artifact_claim_seen else "verification_failed"
        _fail(code, f"No AdapterResult is bound to delivered artifact: {source}")
    if len(matching) != 1:
        _fail("verification_failed", f"Multiple AdapterResults claim delivered artifact: {source}")
    receipt_path, result = matching[0]
    if result.status != "success" or result.operation != "apply":
        _fail("adapter_failed", f"Delivered artifact AdapterResult is not a successful apply: {source}")
    if result.adapter_id != decision.adapter_id:
        _fail(
            "adapter_failed",
            f"AdapterResult adapter_id {result.adapter_id!r} does not match {decision.adapter_id!r}",
        )
    _validate_receipt_lineage(root, result, decision, mod_name)
    artifact = next(
        item
        for item in result.artifacts
        if _claim_key(root, item.path, label="Adapter artifact path") == source_key
    )
    if artifact.sha256 != source_hash:
        _fail("verification_failed", f"AdapterResult artifact hash mismatch: {source}")
    evidence = _validate_evidence(root, result, context, decision, source_key)
    return [relative_posix_path(root, receipt_path), *evidence]


def _ba2_read_evidence(
    root: Path,
    row: dict[str, Any],
    context: GameContext,
    decision: CapabilityDecision,
    mod_name: str,
    manifest_cache: dict[Path, tuple[bool, list[str], dict[str, Any] | None]],
    receipt_paths: list[Path],
    receipt_text_cache: dict[Path, str],
    receipt_result_cache: dict[Path, AdapterResult],
) -> list[str]:
    archive_value = str(row.get("archive_path", "")).replace("\\", "/")
    manifest_value = str(row.get("archive_manifest", "")).replace("\\", "/")
    entry_value = str(row.get("archive_entry_path", "")).replace("\\", "/")
    if not archive_value or not manifest_value or not entry_value:
        _fail("verification_failed", "BA2 loose override provenance is missing archive evidence")
    try:
        archive_path = resolve_project_path(root, archive_value, must_exist=True)
        manifest_path = resolve_project_path(root, manifest_value, must_exist=True)
        _validate_no_reparse_chain(archive_path, root)
        _validate_no_reparse_chain(manifest_path, root)
        validate_regular_path_under(archive_path, root, kind="file", label="BA2 source archive")
        validate_regular_path_under(manifest_path, root, kind="file", label="BA2 audit manifest")
        require_under_any(
            manifest_path,
            [root / "out" / mod_name / "archive_audits"],
            "BA2 audit manifest",
        )
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    if archive_path.suffix.casefold() != ".ba2":
        _fail("verification_failed", f"BA2 provenance archive is not .ba2: {archive_value}")
    archive_hash = sha256_file(archive_path)
    if str(row.get("archive_sha256", "")).casefold() != archive_hash:
        _fail("verification_failed", f"BA2 provenance archive hash mismatch: {archive_value}")

    manifest_result = manifest_cache.get(manifest_path)
    if manifest_result is None:
        manifest_result = verify_ba2_manifest(root, manifest_path)
        manifest_cache[manifest_path] = manifest_result
    passed, manifest_issues, manifest = manifest_result
    if not passed or not isinstance(manifest, dict):
        _fail(
            "verification_failed",
            "BA2 audit manifest failed independent verification: " + "; ".join(manifest_issues),
        )
    if (
        str(manifest.get("game_id", "")) != context.game_id
        or str(manifest.get("ModName", "")) != mod_name
        or str(manifest.get("ArchivePath", "")).replace("\\", "/") != archive_value
        or str(manifest.get("ArchiveSha256", "")).casefold() != archive_hash
    ):
        _fail("profile_error", "BA2 manifest identity does not match final delivery provenance")
    files = manifest.get("Files", [])
    if not isinstance(files, list):
        _fail("verification_failed", "BA2 manifest Files must be an array")
    matching_rows = [
        item
        for item in files
        if isinstance(item, dict)
        and str(item.get("RelativePath", "")).replace("\\", "/") == entry_value
    ]
    if len(matching_rows) != 1:
        _fail("verification_failed", f"BA2 manifest entry is missing or duplicated: {entry_value}")
    manifest_row = matching_rows[0]
    extracted_value = str(manifest_row.get("ProjectPath", "")).replace("\\", "/")
    try:
        extracted_path = resolve_project_path(root, extracted_value, must_exist=True)
        _validate_no_reparse_chain(extracted_path, root)
        validate_regular_path_under(extracted_path, root, kind="file", label="BA2 extracted source")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    extracted_hash = sha256_file(extracted_path)
    expected_entry_hash = str(row.get("archive_entry_sha256", "")).casefold()
    if (
        not expected_entry_hash
        or str(manifest_row.get("Sha256", "")).casefold() != expected_entry_hash
        or extracted_hash != expected_entry_hash
    ):
        _fail("verification_failed", f"BA2 extracted entry hash mismatch: {entry_value}")

    extracted_key = _claim_key(root, extracted_value, label="BA2 extracted source path")
    matching_receipts: list[tuple[Path, AdapterResult]] = []
    for receipt_path in receipt_paths:
        receipt_text = receipt_text_cache.get(receipt_path)
        if receipt_text is None:
            receipt_text = _read_bounded_text(
                receipt_path,
                label="AdapterResult",
                max_bytes=MAX_RECEIPT_BYTES,
            )
            receipt_text_cache[receipt_path] = receipt_text
        if not _receipt_text_claims_path(root, receipt_text, extracted_key):
            continue
        result = receipt_result_cache.get(receipt_path)
        if result is None:
            result = _adapter_result(
                root,
                _read_json_object(receipt_path, label="AdapterResult", text=receipt_text),
                receipt_path,
            )
            receipt_result_cache[receipt_path] = result
        if any(
            _claim_key(root, artifact.path, label="Adapter artifact path") == extracted_key
            for artifact in result.artifacts
        ):
            matching_receipts.append((receipt_path, result))
    if len(matching_receipts) != 1:
        _fail(
            "verification_failed",
            f"Expected exactly one BA2 AdapterResult for extracted entry: {entry_value}",
        )
    receipt_path, result = matching_receipts[0]
    if (
        result.status != "success"
        or result.operation != "extract"
        or result.adapter_id != decision.adapter_id
    ):
        _fail("adapter_failed", f"BA2 AdapterResult does not prove a successful extract: {entry_value}")
    artifact = next(
        artifact
        for artifact in result.artifacts
        if _claim_key(root, artifact.path, label="Adapter artifact path") == extracted_key
    )
    if artifact.sha256 != extracted_hash:
        _fail("verification_failed", f"BA2 AdapterResult artifact hash mismatch: {entry_value}")
    manifest_key = _claim_key(root, manifest_value, label="BA2 manifest path")
    if manifest_key not in {
        _claim_key(root, value, label="Adapter evidence path")
        for value in result.evidence_files
    }:
        _fail("verification_failed", "BA2 AdapterResult does not reference the verified manifest")
    return sorted(
        {
            relative_posix_path(root, receipt_path),
            relative_posix_path(root, manifest_path),
        },
        key=str.casefold,
    )


def _bsa_read_evidence(
    root: Path,
    row: dict[str, Any],
    decision: CapabilityDecision,
    mod_name: str,
    manifest_cache: dict[Path, dict[str, Any]],
    receipt_paths: list[Path],
    receipt_text_cache: dict[Path, str],
    receipt_result_cache: dict[Path, AdapterResult],
) -> list[str]:
    archive_value = str(row.get("archive_path", "")).replace("\\", "/")
    manifest_value = str(row.get("archive_manifest", "")).replace("\\", "/")
    entry_value = str(row.get("archive_entry_path", "")).replace("\\", "/")
    if not archive_value or not manifest_value or not entry_value:
        _fail("verification_failed", "BSA loose override provenance is missing archive evidence")
    try:
        canonical_entry = validate_archive_relative_path(entry_value)
        if canonical_entry != entry_value:
            raise ValueError("BSA archive entry path is not canonical")
        archive_path = resolve_project_path(root, archive_value, must_exist=True)
        manifest_path = resolve_project_path(root, manifest_value, must_exist=True)
        _validate_no_reparse_chain(archive_path, root)
        _validate_no_reparse_chain(manifest_path, root)
        validate_regular_path_under(archive_path, root, kind="file", label="BSA source archive")
        validate_regular_path_under(manifest_path, root, kind="file", label="BSA audit manifest")
        require_under_any(
            archive_path,
            [root / "mod", root / "work" / "extracted_mods"],
            "BSA source archive",
        )
        require_under_any(
            manifest_path,
            [root / "out" / mod_name / "archive_audits"],
            "BSA audit manifest",
        )
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    if archive_path.suffix.casefold() != ".bsa":
        _fail("verification_failed", f"BSA provenance archive is not .bsa: {archive_value}")
    archive_hash = sha256_file(archive_path)
    if str(row.get("archive_sha256", "")).casefold() != archive_hash:
        _fail("verification_failed", f"BSA provenance archive hash mismatch: {archive_value}")

    manifest = manifest_cache.get(manifest_path)
    if manifest is None:
        manifest = _read_json_object(manifest_path, label="BSA audit manifest")
        manifest_cache[manifest_path] = manifest
    if manifest.get("schema") == "skyrim-mod-chs.ba2-extraction-manifest":
        _fail("verification_failed", "BSA evidence cannot use the BA2 extraction manifest schema")
    if (
        str(manifest.get("ModName", "")) != mod_name
        or str(manifest.get("ArchivePath", "")).replace("\\", "/") != archive_value
        or str(manifest.get("ArchiveSha256", "")).casefold() != archive_hash
        or type(manifest.get("ArchiveSize")) is not int
        or manifest.get("ArchiveSize") != archive_path.stat().st_size
    ):
        _fail("verification_failed", "BSA manifest identity does not match final delivery provenance")
    expected_safety = {
        "ProjectLocalOnly": True,
        "ArchiveModified": False,
        "ExtractedContentModified": False,
        "RealGameDirectoriesAccessed": False,
    }
    safety = manifest.get("Safety")
    if not isinstance(safety, dict) or any(safety.get(key) is not value for key, value in expected_safety.items()):
        _fail("verification_failed", "BSA manifest safety claims are missing or invalid")
    try:
        extracted_value = str(manifest.get("ExtractedDir", "")).replace("\\", "/")
        extracted_dir = resolve_project_path(root, extracted_value, must_exist=True)
        _validate_no_reparse_chain(extracted_dir, root)
        require_under_any(extracted_dir, [root / "work" / "archive_extracts"], "BSA extracted directory")
        if not extracted_dir.is_dir():
            raise ValueError("BSA ExtractedDir is not a directory")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))

    files = manifest.get("Files")
    if not isinstance(files, list) or manifest.get("FilesScanned") != len(files):
        _fail("verification_failed", "BSA manifest file count is invalid")
    matching_rows = [
        item
        for item in files
        if isinstance(item, dict)
        and str(item.get("RelativePath", "")).replace("\\", "/") == entry_value
    ]
    if len(matching_rows) != 1:
        _fail("verification_failed", f"BSA manifest entry is missing or duplicated: {entry_value}")
    manifest_row = matching_rows[0]
    extracted_project_value = str(manifest_row.get("ProjectPath", "")).replace("\\", "/")
    try:
        extracted_path = resolve_project_path(root, extracted_project_value, must_exist=True)
        expected_path = (extracted_dir / Path(*entry_value.split("/"))).resolve(strict=True)
        _validate_no_reparse_chain(extracted_path, root)
        validate_regular_path_under(extracted_path, root, kind="file", label="BSA extracted source")
        if extracted_path != expected_path:
            raise ValueError("BSA manifest ProjectPath does not match archive entry path")
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    extracted_hash = sha256_file(extracted_path)
    expected_entry_hash = str(row.get("archive_entry_sha256", "")).casefold()
    manifest_entry_hash = manifest_row.get("Sha256")
    if (
        not expected_entry_hash
        or extracted_hash != expected_entry_hash
        or (manifest_entry_hash is not None and str(manifest_entry_hash).casefold() != expected_entry_hash)
        or type(manifest_row.get("Size")) is not int
        or manifest_row.get("Size") != extracted_path.stat().st_size
    ):
        _fail("verification_failed", f"BSA extracted entry hash or size mismatch: {entry_value}")

    extracted_key = _claim_key(root, extracted_project_value, label="BSA extracted source path")
    matching_receipts: list[tuple[Path, AdapterResult]] = []
    for receipt_path in receipt_paths:
        receipt_text = receipt_text_cache.get(receipt_path)
        if receipt_text is None:
            receipt_text = _read_bounded_text(
                receipt_path,
                label="AdapterResult",
                max_bytes=MAX_RECEIPT_BYTES,
            )
            receipt_text_cache[receipt_path] = receipt_text
        if not _receipt_text_claims_path(root, receipt_text, extracted_key):
            continue
        result = receipt_result_cache.get(receipt_path)
        if result is None:
            result = _adapter_result(
                root,
                _read_json_object(receipt_path, label="AdapterResult", text=receipt_text),
                receipt_path,
            )
            receipt_result_cache[receipt_path] = result
        if any(
            _claim_key(root, artifact.path, label="Adapter artifact path") == extracted_key
            for artifact in result.artifacts
        ):
            matching_receipts.append((receipt_path, result))
    if len(matching_receipts) != 1:
        _fail(
            "verification_failed",
            f"Expected exactly one BSA AdapterResult for extracted entry: {entry_value}",
        )
    receipt_path, result = matching_receipts[0]
    if (
        result.status != "success"
        or result.operation != "extract"
        or result.adapter_id != decision.adapter_id
        or result.mod_name != mod_name
    ):
        _fail("adapter_failed", f"BSA AdapterResult does not prove a successful extract: {entry_value}")
    artifact = next(
        artifact
        for artifact in result.artifacts
        if _claim_key(root, artifact.path, label="Adapter artifact path") == extracted_key
    )
    if artifact.sha256 != extracted_hash:
        _fail("verification_failed", f"BSA AdapterResult artifact hash mismatch: {entry_value}")
    if len(result.inputs) != 1:
        _fail("verification_failed", "BSA AdapterResult must bind exactly one source archive input")
    archive_input = result.inputs[0]
    archive_key = _claim_key(root, archive_value, label="BSA source archive path")
    if _claim_key(root, archive_input.path, label="Adapter input path") != archive_key:
        _fail("verification_failed", "BSA AdapterResult source archive input does not match provenance")
    try:
        receipt_archive_path = resolve_project_path(root, archive_input.path, must_exist=True)
        _validate_no_reparse_chain(receipt_archive_path, root)
        validate_regular_path_under(
            receipt_archive_path,
            root,
            kind="file",
            label="BSA AdapterResult source archive input",
        )
    except (OSError, ValueError) as exc:
        _fail("verification_failed", str(exc))
    if receipt_archive_path != archive_path or archive_input.sha256 != archive_hash:
        _fail("verification_failed", "BSA AdapterResult source archive input hash mismatch")
    manifest_key = _claim_key(root, manifest_value, label="BSA manifest path")
    if manifest_key not in {
        _claim_key(root, value, label="Adapter evidence path")
        for value in result.evidence_files
    }:
        _fail("verification_failed", "BSA AdapterResult does not reference the verified manifest")
    return sorted(
        {
            relative_posix_path(root, receipt_path),
            relative_posix_path(root, manifest_path),
        },
        key=str.casefold,
    )


def collect_used_capabilities(
    root: Path,
    mod_name: str,
    final_mod_dir: Path,
) -> dict[str, object]:
    root = root.resolve(strict=True)
    normalized_mod_name = mod_name.strip()
    if not normalized_mod_name:
        _fail("profile_error", "mod_name must be non-empty")
    try:
        safe_mod_name = safe_file_name(normalized_mod_name)
        if safe_mod_name != normalized_mod_name:
            raise ValueError("mod_name must already be a canonical safe file name")
        lexical_final_mod = _absolute_lexical(final_mod_dir if final_mod_dir.is_absolute() else root / final_mod_dir)
        _validate_no_reparse_chain(lexical_final_mod, root)
        final_mod = resolve_project_path(root, final_mod_dir, must_exist=True)
        expected_final_mod = canonical_final_mod_dir(root, safe_mod_name).resolve(strict=False)
        if final_mod != expected_final_mod:
            raise ValueError(
                f"FinalModDir must be the canonical output for mod {safe_mod_name!r}: "
                f"{expected_final_mod}"
            )
        context = load_game_context(root)
    except (OSError, ValueError) as exc:
        _fail("profile_error", str(exc))
    provenance_path, rows = _read_provenance(root, final_mod, context)
    provenance_evidence = relative_posix_path(root, provenance_path)
    receipt_paths: list[Path] | None = None
    receipt_text_cache: dict[Path, str] = {}
    receipt_result_cache: dict[Path, AdapterResult] = {}
    ba2_manifest_cache: dict[Path, tuple[bool, list[str], dict[str, Any] | None]] = {}
    bsa_manifest_cache: dict[Path, dict[str, Any]] = {}
    merged: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        for capability, operation in _row_capabilities(row):
            relative_resource = _resource_relative_path(row, capability, final_mod)
            resource = classify_resource(context, relative_resource)
            if resource.capability != capability:
                _fail(
                    "verification_failed",
                    f"Resource {relative_resource.as_posix()} resolves capability "
                    f"{resource.capability!r}, not delivered capability {capability!r}",
                )
            decision = _decision(context, capability, operation, resource)
            evidence = [provenance_evidence]
            if capability == "loose_text":
                _source_artifact(
                    root,
                    row,
                    capability=capability,
                    mod_name=safe_mod_name,
                    final_mod=final_mod,
                )
            elif capability == "archive.ba2":
                if receipt_paths is None:
                    receipt_paths = _receipt_paths(root, safe_mod_name)
                evidence = [
                    *_ba2_read_evidence(
                        root,
                        row,
                        context,
                        decision,
                        safe_mod_name,
                        ba2_manifest_cache,
                        receipt_paths,
                        receipt_text_cache,
                        receipt_result_cache,
                    ),
                    provenance_evidence,
                ]
            elif capability == "archive.bsa":
                if receipt_paths is None:
                    receipt_paths = _receipt_paths(root, safe_mod_name)
                evidence = [
                    *_bsa_read_evidence(
                        root,
                        row,
                        decision,
                        safe_mod_name,
                        bsa_manifest_cache,
                        receipt_paths,
                        receipt_text_cache,
                        receipt_result_cache,
                    ),
                    provenance_evidence,
                ]
            else:
                if receipt_paths is None:
                    receipt_paths = _receipt_paths(root, safe_mod_name)
                bound_evidence = _bound_receipt_evidence(
                    root,
                    row,
                    context,
                    decision,
                    safe_mod_name,
                    final_mod,
                    receipt_paths,
                    receipt_text_cache,
                    receipt_result_cache,
                )
                if capability == "plugin_text":
                    report_traits = _plugin_traits_from_evidence(root, bound_evidence)
                    unknown_write_traits = (
                        unknown_write_plugin_trait_fields(context, report_traits)
                        if operation == "write"
                        else ()
                    )
                    if unknown_write_traits:
                        _fail(
                            "plugin_trait_unknown",
                            "Plugin write evidence has unknown header traits: "
                            + ", ".join(unknown_write_traits),
                        )
                    resource = classify_resource(
                        context,
                        relative_resource,
                        traits=report_traits.resource_traits(),
                    )
                    decision = _decision(context, capability, operation, resource)
                evidence = [*bound_evidence, provenance_evidence]
            key = (
                capability,
                operation,
                decision.level,
                str(decision.adapter_id),
                resource.category,
                resource.subtype,
                resource.container,
                tuple(sorted(resource.traits)),
                resource.relative_path.as_posix(),
            )
            resource_record = capability_evidence(resource, decision)
            record = merged.setdefault(
                key,
                {
                    **resource_record,
                    "name": capability,
                    "level": decision.level,
                    "adapter_id": decision.adapter_id,
                    "result": "success",
                    "participates_in_final_delivery": True,
                    "evidence": [],
                },
            )
            current = record["evidence"]
            assert isinstance(current, list)
            current.extend(evidence)
    records = list(merged.values())
    for record in records:
        values = record["evidence"]
        assert isinstance(values, list)
        record["evidence"] = sorted(set(values), key=str.casefold)
    records.sort(
        key=lambda item: (
            str(item["name"]).casefold(),
            str(item["operation"]).casefold(),
            str(item["adapter_id"]).casefold(),
        )
    )
    summary_merged: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for operation_record in records:
        summary_key = (
            str(operation_record["name"]),
            str(operation_record["operation"]),
            str(operation_record["level"]),
            str(operation_record["adapter_id"]),
        )
        summary = summary_merged.setdefault(
            summary_key,
            {
                "name": operation_record["name"],
                "operation": operation_record["operation"],
                "level": operation_record["level"],
                "adapter_id": operation_record["adapter_id"],
                "result": operation_record["result"],
                "strict_complete_allowed": operation_record["strict_complete_allowed"],
                "participates_in_final_delivery": operation_record[
                    "participates_in_final_delivery"
                ],
                "evidence": [],
            },
        )
        summary_evidence = summary["evidence"]
        operation_evidence = operation_record["evidence"]
        assert isinstance(summary_evidence, list)
        assert isinstance(operation_evidence, list)
        summary_evidence.extend(operation_evidence)
    summaries = list(summary_merged.values())
    for summary in summaries:
        summary_evidence = summary["evidence"]
        assert isinstance(summary_evidence, list)
        summary["evidence"] = sorted(set(summary_evidence), key=str.casefold)
    summaries.sort(
        key=lambda item: (
            str(item["name"]).casefold(),
            str(item["operation"]).casefold(),
            str(item["adapter_id"]).casefold(),
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "game_id": context.game_id,
        "mod_name": safe_mod_name,
        "capabilities": summaries,
        "operations": records,
    }


def write_used_capabilities(
    root: Path,
    mod_name: str,
    final_mod_dir: Path,
    output_path: Path | None = None,
) -> Path:
    root = root.resolve(strict=True)
    try:
        normalized_mod_name = mod_name.strip()
        safe_mod_name = safe_file_name(normalized_mod_name)
        if not normalized_mod_name or safe_mod_name != normalized_mod_name:
            raise ValueError("mod_name must already be a canonical safe file name")
        expected_output = (root / "qa" / f"{safe_mod_name}.used_capabilities.json").resolve(
            strict=False
        )
        raw_output = output_path or expected_output
        output = resolve_project_path(root, raw_output, must_exist=False)
        if output != expected_output:
            raise ValueError(
                f"UsedCapabilitiesOutput must be the canonical report for mod "
                f"{safe_mod_name!r}: {expected_output}"
            )
    except ValueError as exc:
        _fail("profile_error", str(exc))
    if output.suffix.casefold() != ".json":
        _fail("profile_error", "Used capabilities output must be a .json file")
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_reparse_chain(output.parent, root)
    output.unlink(missing_ok=True)
    payload = collect_used_capabilities(root, mod_name, final_mod_dir)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _validate_no_reparse_chain(output.parent, root)
        os.replace(temporary, output)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Write actual final-delivery capability evidence.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--final-mod-dir", required=True)
    parser.add_argument("--output-path", default="")
    args = parser.parse_args()
    root = project_root()
    output = write_used_capabilities(
        root,
        args.mod_name,
        Path(args.final_mod_dir),
        Path(args.output_path) if args.output_path else None,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
