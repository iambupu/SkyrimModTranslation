"""Stable public result contract shared by SMT CLI entry points."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypedDict


SCHEMA_VERSION = 1

PublicOutcome: TypeAlias = Literal[
    "completed",
    "ready_for_manual_test",
    "needs_gui",
    "needs_agent_translation",
    "needs_user_input",
    "blocked",
]

EXIT_SUCCESS = 0
EXIT_INTERNAL_READ_OR_BUSY = 1
EXIT_SAFE_STOP = 3
EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY = 4
EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE = 5
EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT = 6
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130


class ArtifactInfo(TypedDict):
    """A workspace artifact that supports a next action."""

    path: str
    exists: bool
    kind: str
    validated: bool | None
    validation_evidence: str | None


class NextAction(TypedDict):
    """The one public action a caller should take next."""

    kind: str
    summary: str
    artifacts: list[str]


@dataclass
class CliResult:
    """Schema v1 payload returned by every public SMT CLI command."""

    schema_version: int = SCHEMA_VERSION
    command: str = ""
    outcome: PublicOutcome | None = None
    exit_code: int = EXIT_INTERNAL_READ_OR_BUSY
    message: str = ""
    workspace: str | None = None
    mod_name: str | None = None
    game_id: str | None = None
    workflow_state: str | None = None
    state_snapshot: bool = False
    state_generated_at: str | None = None
    state_generated_at_timezone: str | None = None
    refreshed_by_this_command: bool = False
    busy: bool = False
    next_action: NextAction | None = None
    progress_card_path: str | None = None
    progress_card: str | None = None
    output_paths: dict[str, ArtifactInfo] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    diagnostic_log_path: str | None = None
    underlying_exit_codes: list[int] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of the fixed schema v1 payload."""

        return _json_serializable(asdict(self))


def empty_result(command: str) -> CliResult:
    """Create an unpopulated result without emitting command output."""

    return CliResult(command=command)


def _json_serializable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_serializable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
