from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


ADAPTER_OPERATIONS = ("inventory", "extract", "apply", "verify")
ADAPTER_RESULT_STATUSES = ("success", "error", "blocked")
BUILTIN_HANDLERS = frozenset(
    {
        "resource-inventory",
        "archive-inventory",
        "archive-manifest",
        "loose-text",
        "string-tables",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILTIN_ENTRYPOINT_RE = re.compile(r"^builtin:[a-z0-9]+(?:-[a-z0-9]+)*$")


def _non_empty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _freeze_text_items(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a collection of non-empty strings")
    try:
        normalized = tuple(_non_empty_text(value, label) for value in values)
    except TypeError as exc:
        raise ValueError(f"{label} must be a collection of non-empty strings") from exc
    return normalized


def validate_entrypoint(entrypoint: str, scripts_dir: Path | None = None) -> str:
    normalized = _non_empty_text(entrypoint, "Adapter entrypoint")
    if normalized.startswith("builtin:"):
        handler = normalized.removeprefix("builtin:")
        if (
            not _BUILTIN_ENTRYPOINT_RE.fullmatch(normalized)
            or handler not in BUILTIN_HANDLERS
        ):
            raise ValueError(f"Adapter entrypoint has invalid builtin handler: {entrypoint!r}")
        return normalized

    if (
        Path(normalized).is_absolute()
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or not normalized.endswith(".py")
        or Path(normalized).name != normalized
    ):
        raise ValueError(
            "Adapter entrypoint must be a plain relative Python filename under scripts/"
        )
    if scripts_dir is not None and not (scripts_dir / normalized).is_file():
        raise ValueError(f"Adapter entrypoint script does not exist: scripts/{normalized}")
    return normalized


@dataclass(frozen=True)
class AdapterArtifact:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _non_empty_text(self.path, "Adapter artifact path"))
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("Adapter artifact sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class AdapterResult:
    status: str
    error_code: str | None
    operation: str
    adapter_id: str
    artifacts: tuple[AdapterArtifact, ...]
    evidence_files: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    mod_name: str = ""
    inputs: tuple[AdapterArtifact, ...] = ()

    def __post_init__(self) -> None:
        status = _non_empty_text(self.status, "Adapter result status")
        if status not in ADAPTER_RESULT_STATUSES:
            supported = ", ".join(ADAPTER_RESULT_STATUSES)
            raise ValueError(f"Invalid adapter result status {status!r}; expected one of: {supported}")
        operation = _non_empty_text(self.operation, "Adapter result operation")
        if operation not in ADAPTER_OPERATIONS:
            supported = ", ".join(ADAPTER_OPERATIONS)
            raise ValueError(
                f"Invalid adapter result operation {operation!r}; expected one of: {supported}"
            )
        adapter_id = _non_empty_text(self.adapter_id, "Adapter result adapter_id")
        if self.error_code is not None:
            error_code = _non_empty_text(self.error_code, "Adapter result error_code")
        else:
            error_code = None
        if status == "success" and error_code is not None:
            raise ValueError("Successful adapter result error_code must be None")
        if status != "success" and error_code is None:
            raise ValueError("Non-success adapter result error_code must be non-empty")

        try:
            artifacts = tuple(self.artifacts)
        except TypeError as exc:
            raise ValueError("Adapter result artifacts must be a collection") from exc
        if any(not isinstance(artifact, AdapterArtifact) for artifact in artifacts):
            raise ValueError("Adapter result artifacts must contain AdapterArtifact values")
        evidence_files = _freeze_text_items(
            self.evidence_files,
            "Adapter result evidence file",
        )
        warnings = _freeze_text_items(self.warnings, "Adapter result warning")
        blockers = _freeze_text_items(self.blockers, "Adapter result blocker")
        mod_name = self.mod_name.strip() if isinstance(self.mod_name, str) else ""
        if mod_name and (Path(mod_name).name != mod_name or mod_name in {".", ".."}):
            raise ValueError("Adapter result mod_name must be a plain workspace lane name")
        try:
            inputs = tuple(self.inputs)
        except TypeError as exc:
            raise ValueError("Adapter result inputs must be a collection") from exc
        if any(not isinstance(item, AdapterArtifact) for item in inputs):
            raise ValueError("Adapter result inputs must contain AdapterArtifact values")
        if bool(mod_name) != bool(inputs):
            raise ValueError("Adapter result mod_name and inputs must be provided together")
        input_paths = [item.path.casefold() for item in inputs]
        if len(input_paths) != len(set(input_paths)):
            raise ValueError("Adapter result inputs must not contain duplicate paths")
        if status == "success" and blockers:
            raise ValueError("Successful adapter result blockers must be empty")
        if status == "blocked" and not blockers:
            raise ValueError("Blocked adapter result must include at least one blocker")
        if status == "success" and operation == "apply":
            if not artifacts:
                raise ValueError("Successful apply result must include at least one artifact")
            if not evidence_files:
                raise ValueError("Successful apply result must include at least one evidence file")

        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(
            self,
            "evidence_files",
            evidence_files,
        )
        object.__setattr__(
            self,
            "warnings",
            warnings,
        )
        object.__setattr__(
            self,
            "blockers",
            blockers,
        )
        object.__setattr__(self, "mod_name", mod_name)
        object.__setattr__(self, "inputs", inputs)


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    entrypoints: Mapping[str, str]
    required_options: tuple[str, ...]

    def __post_init__(self) -> None:
        adapter_id = _non_empty_text(self.adapter_id, "AdapterSpec adapter_id")
        if not isinstance(self.entrypoints, Mapping) or not self.entrypoints:
            raise ValueError("AdapterSpec entrypoints must be a non-empty mapping")
        entrypoints: dict[str, str] = {}
        operation_rows: list[tuple[str, str, str]] = []
        operations_by_casefold: dict[str, str] = {}
        for operation, entrypoint in self.entrypoints.items():
            normalized_operation = _non_empty_text(operation, "AdapterSpec operation")
            operation_casefold = normalized_operation.casefold()
            previous_operation = operations_by_casefold.get(operation_casefold)
            if previous_operation is not None:
                raise ValueError(
                    "AdapterSpec operation normalization/casefold collision: "
                    f"{previous_operation!r}, {operation!r}"
                )
            operations_by_casefold[operation_casefold] = operation
            operation_rows.append((operation, normalized_operation, entrypoint))

        for operation, normalized_operation, entrypoint in operation_rows:
            if operation != normalized_operation:
                raise ValueError(
                    f"AdapterSpec operation must not contain surrounding whitespace: {operation!r}"
                )
            if normalized_operation not in ADAPTER_OPERATIONS:
                raise ValueError(f"AdapterSpec has unknown operation: {normalized_operation!r}")
            entrypoints[normalized_operation] = validate_entrypoint(entrypoint)

        required_options = _freeze_text_items(
            self.required_options,
            "AdapterSpec required option",
        )
        if len(set(required_options)) != len(required_options):
            raise ValueError("AdapterSpec required_options must not contain duplicates")

        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "entrypoints", MappingProxyType(entrypoints))
        object.__setattr__(self, "required_options", tuple(sorted(required_options)))


def validate_adapter_result(result: AdapterResult) -> AdapterResult:
    if not isinstance(result, AdapterResult):
        raise ValueError("result must be an AdapterResult")
    try:
        rebuilt_artifacts = tuple(
            AdapterArtifact(path=artifact.path, sha256=artifact.sha256)
            for artifact in result.artifacts
        )
        rebuilt = AdapterResult(
            status=result.status,
            error_code=result.error_code,
            operation=result.operation,
            adapter_id=result.adapter_id,
            artifacts=rebuilt_artifacts,
            evidence_files=result.evidence_files,
            warnings=result.warnings,
            blockers=result.blockers,
            mod_name=result.mod_name,
            inputs=tuple(
                AdapterArtifact(path=item.path, sha256=item.sha256)
                for item in result.inputs
            ),
        )
    except Exception as exc:
        raise ValueError(f"AdapterResult validation failed: {exc}") from exc
    if rebuilt != result:
        raise ValueError("AdapterResult fields are not in canonical immutable form")
    return result
