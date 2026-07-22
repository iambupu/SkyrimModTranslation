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
EXIT_FAILURE = 1
EXIT_WORKSPACE_ERROR = 3
EXIT_BUSY = 4
EXIT_NEEDS_GUI = 5
EXIT_NEEDS_AGENT_TRANSLATION = 6
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
    artifacts: list[ArtifactInfo]


@dataclass
class CliResult:
    """Schema v1 payload returned by every public SMT CLI command."""

    schema_version: int = SCHEMA_VERSION
    command: str = ""
    outcome: PublicOutcome = "blocked"
    exit_code: int = EXIT_FAILURE
    message: str | None = None
    workspace: str | None = None
    mod_name: str | None = None
    game_id: str | None = None
    workflow_state: str | None = None
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    state_generated_at: str | None = None
    state_generated_at_timezone: str | None = None
    refreshed_by_this_command: bool = False
    busy: bool = False
    next_action: NextAction | None = None
    progress_card_path: str | None = None
    progress_card: str | None = None
    output_paths: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    diagnostic_log_path: str | None = None
    underlying_exit_codes: dict[str, int] = field(default_factory=dict)

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
