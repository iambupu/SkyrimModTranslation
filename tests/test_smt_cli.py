"""Regression tests for the stable SMT CLI result contract."""

from __future__ import annotations

import json
import hashlib
import importlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import get_args, get_type_hints

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import smt_cli  # noqa: E402
import smt_windows  # noqa: E402
import workflow_agent_log  # noqa: E402
import write_workflow_state  # noqa: E402
from smt_windows import ProcessResult  # noqa: E402
from workflow_refresh import CORE_REFRESH_STEPS  # noqa: E402


EXPECTED_PAYLOAD_KEYS = {
    "schema_version",
    "command",
    "outcome",
    "exit_code",
    "message",
    "workspace",
    "mod_name",
    "game_id",
    "workflow_state",
    "state_snapshot",
    "state_generated_at",
    "state_generated_at_timezone",
    "refreshed_by_this_command",
    "busy",
    "next_action",
    "progress_card_path",
    "progress_card",
    "output_paths",
    "details",
    "diagnostics",
    "diagnostic_log_path",
    "underlying_exit_codes",
}


@pytest.fixture
def cli_safe_tmp_path() -> Path:
    with tempfile.TemporaryDirectory(
        prefix="pytest-smt-cli-",
        dir=REPOSITORY_ROOT.parent,
    ) as temp_dir:
        yield Path(temp_dir)


def test_empty_result_has_the_complete_v1_payload_and_is_json_serializable() -> None:
    result = smt_cli.empty_result("status")
    payload = result.to_payload()
    result_hints = get_type_hints(smt_cli.CliResult)

    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["schema_version"] == 1
    assert payload["command"] == "status"
    assert payload["outcome"] is None
    assert payload["exit_code"] == smt_cli.EXIT_INTERNAL_READ_OR_BUSY
    assert payload["message"] == ""
    assert payload["state_snapshot"] is False
    assert payload["output_paths"] == {}
    assert payload["details"] == []
    assert payload["diagnostics"] == []
    assert payload["underlying_exit_codes"] == []
    assert type(payload["message"]) is str
    assert type(payload["state_snapshot"]) is bool
    assert type(payload["output_paths"]) is dict
    assert type(payload["details"]) is list
    assert type(payload["diagnostics"]) is list
    assert type(payload["underlying_exit_codes"]) is list
    assert type(payload["exit_code"]) is int
    assert result_hints["outcome"] == smt_cli.PublicOutcome | None
    assert result_hints["message"] is str
    assert result_hints["state_snapshot"] is bool
    assert result_hints["output_paths"] == dict[str, smt_cli.ArtifactInfo]
    assert result_hints["details"] == list[str]
    assert result_hints["diagnostics"] == list[str]
    assert result_hints["underlying_exit_codes"] == list[int]
    assert get_args(smt_cli.PublicOutcome) == (
        "completed",
        "ready_for_manual_test",
        "needs_gui",
        "needs_agent_translation",
        "needs_user_input",
        "blocked",
    )
    assert json.dumps(payload)


def test_windows_workspace_string_is_preserved_in_json_payload() -> None:
    result = smt_cli.empty_result("status")
    result.workspace = r"D:\mods\Example Workspace"
    payload = result.to_payload()

    assert payload["workspace"] == r"D:\mods\Example Workspace"
    assert json.loads(json.dumps(payload))["workspace"] == r"D:\mods\Example Workspace"


def test_next_action_artifacts_follow_the_public_artifact_structure() -> None:
    artifact: smt_cli.ArtifactInfo = {
        "path": r"D:\mods\Example Workspace\qa\workflow_state.json",
        "exists": True,
        "kind": "workflow_state",
        "validated": None,
        "validation_evidence": None,
    }
    result = smt_cli.empty_result("status")
    result.output_paths = {"final_mod": artifact}
    result.next_action = {
        "kind": "review_state",
        "summary": "Read the workflow state.",
        "artifacts": [artifact["path"]],
    }

    payload = result.to_payload()

    assert set(smt_cli.ArtifactInfo.__annotations__) == {
        "path",
        "exists",
        "kind",
        "validated",
        "validation_evidence",
    }
    assert get_type_hints(smt_cli.NextAction)["artifacts"] == list[str]
    assert payload["output_paths"] == {"final_mod": artifact}
    assert payload["next_action"] == {
        "kind": "review_state",
        "summary": "Read the workflow state.",
        "artifacts": [artifact["path"]],
    }
    assert json.dumps(payload)


def test_public_exit_codes_have_the_documented_semantics() -> None:
    assert smt_cli.EXIT_SUCCESS == 0
    assert smt_cli.EXIT_INTERNAL_READ_OR_BUSY == 1
    assert smt_cli.EXIT_SAFE_STOP == 3
    assert smt_cli.EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY == 4
    assert smt_cli.EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE == 5
    assert smt_cli.EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT == 6
    assert smt_cli.EXIT_TIMEOUT == 124
    assert smt_cli.EXIT_INTERRUPTED == 130


def test_to_payload_rejects_unknown_message_values() -> None:
    result = smt_cli.empty_result("status")
    result.message = object()  # type: ignore[assignment]

    with pytest.raises(TypeError, match=r"\$\.message"):
        result.to_payload()


def test_to_payload_rejects_path_values_in_path_fields() -> None:
    result = smt_cli.empty_result("status")
    result.workspace = Path(r"D:\mods\Example Workspace")  # type: ignore[assignment]

    with pytest.raises(TypeError, match=r"\$\.workspace"):
        result.to_payload()


def test_module_import_and_empty_result_do_not_write_to_standard_streams() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(SCRIPTS_DIRECTORY)!r}); "
                "import smt_cli; smt_cli.empty_result('status')"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def _session(mod_name: str = "ExampleMod") -> smt_cli.SmtSession:
    digest = "1" * 64
    return smt_cli.SmtSession(
        schema_version=1,
        workspace_id="00000000-0000-4000-8000-000000000001",
        mod_name=mod_name,
        game_id="skyrim-se",
        fingerprint_algorithm="smt-input-v1",
        input_identity=f"smt-input-v1:skyrim-se:zip:{digest}",
        source_kind="zip",
        source_display_name=f"{mod_name}.zip",
        source_sha256=digest,
        import_relative_path=f"mod/{mod_name}.zip",
        imported_sha256=digest,
        created_at="2026-07-22T00:00:00+00:00",
    )


def _state_row(
    mod: str = "ExampleMod",
    state: str = "candidates_extracted",
    *,
    blockers: list[str] | None = None,
    next_actions: list[dict[str, object]] | None = None,
    last_attempt: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "mod": mod,
        "state": state,
        "blocking_checks": blockers or [],
        "stop_conditions": [],
        "evidence": {"candidate": f"work/normalized/{mod}/strings.jsonl"},
        "next_actions": next_actions or [],
        "last_attempt": last_attempt or {},
        # A deliberately large value proves SMT does not use this generic count.
        "retry_count": 999,
    }


def _task(
    task_id: str,
    *,
    mod: str = "ExampleMod",
    kind: str = "run_command",
    status: str = "pending",
    executable: bool = True,
    risk: str = "low",
    evidence: str = "qa/evidence.json",
    dependencies: list[str] | None = None,
    resource_locks: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "task_id": task_id,
        "mod": mod,
        "stage": "candidates_extracted",
        "kind": kind,
        "status": status,
        "reason": kind,
        "risk": risk,
        "command": "python scripts/example.py",
        "executable": executable,
        "can_run_parallel": True,
        "dependencies": dependencies or [],
        "resource_locks": resource_locks or [f"mod:{mod}"],
        "evidence": evidence,
    }
    row.update(extra)
    return row


def _snapshot(
    *,
    project_state: str = "candidates_extracted",
    rows: list[dict[str, object]] | None = None,
    tasks: list[dict[str, object]] | None = None,
    project_blockers: list[str] | None = None,
) -> smt_cli.WorkflowSnapshot:
    session = _session()
    state: dict[str, object] = {
        "schema_version": 1,
        "generated_at": "2026-07-22 12:34:56",
        "project_state": project_state,
        "states": rows if rows is not None else [_state_row()],
    }
    if project_blockers is not None:
        state["blocking_checks"] = project_blockers
    return smt_cli.WorkflowSnapshot(
        workspace=Path(r"D:\SMT\Example"),
        marker={"schema_version": 2, "kind": smt_cli.WORKSPACE_KIND, "game_id": "skyrim-se"},
        session=session,
        workflow_state=state,
        workflow_tasks={"schema_version": 1, "tasks": tasks or []},
        progress_card="# [SMT 进度]\n\n原始进度卡\n",
        policy={"agent_orchestration_policy": {"max_same_blocker_attempts": 2}},
    )


@dataclass
class _RecordingRunner:
    exit_codes: list[int]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, argv: object, **kwargs: object) -> ProcessResult:
        self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return ProcessResult(exit_code=code, output_tail=(f"exit={code}",))


def _discard_attempt(**_fields: object) -> None:
    return None


def test_refresh_authoritative_state_uses_the_imported_core_order(tmp_path: Path) -> None:
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))

    codes = smt_cli.refresh_authoritative_state(tmp_path, runner, 60)

    assert codes == [0] * len(CORE_REFRESH_STEPS)
    assert [Path(call["argv"][1]).name for call in runner.calls] == [  # type: ignore[index]
        step.script for step in CORE_REFRESH_STEPS
    ]
    assert all(call["cwd"] == tmp_path for call in runner.calls)
    assert all(call["log_path"] == tmp_path / ".workflow" / "smt-cli.log" for call in runner.calls)


def test_refresh_core_steps_share_one_deadline(tmp_path: Path) -> None:
    current = [0.0]

    class _ElapsedRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            current[0] += 20.0
            return ProcessResult(0, ())

    runner = _ElapsedRunner([])
    codes = smt_cli.refresh_authoritative_state(
        tmp_path,
        runner,
        60,
        deadline=60.0,
        monotonic=lambda: current[0],
    )

    assert codes == [0, 0, 0, smt_cli.EXIT_TIMEOUT]
    assert [call["timeout_seconds"] for call in runner.calls] == [60, 40, 20]


def test_refresh_preserves_each_step_status_and_output_tail(tmp_path: Path) -> None:
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))
    diagnostics: list[str] = []

    codes = smt_cli.refresh_authoritative_state(
        tmp_path, runner, 60, diagnostics=diagnostics
    )

    assert codes == [0, 0, 0, 0]
    for step in CORE_REFRESH_STEPS:
        assert any(step.name in line and "exit=0" in line for line in diagnostics)
    assert sum(line.endswith("exit=0") for line in diagnostics) >= 4


def test_refresh_diagnostics_keep_only_the_last_two_hundred_lines(
    tmp_path: Path,
) -> None:
    class _LargeTailRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            call_number = len(self.calls)
            return ProcessResult(
                0,
                tuple(
                    f"call-{call_number}-line-{line_number}"
                    for line_number in range(201)
                ),
            )

    diagnostics: list[str] = []

    codes = smt_cli.refresh_authoritative_state(
        tmp_path,
        _LargeTailRunner([]),
        60,
        diagnostics=diagnostics,
    )

    assert codes == [0] * len(CORE_REFRESH_STEPS)
    assert len(diagnostics) == 200
    assert diagnostics[-1].endswith(
        f"call-{len(CORE_REFRESH_STEPS)}-line-200"
    )


@pytest.mark.parametrize("error", [OSError("spawn failed"), ValueError("bad codec")])
def test_refresh_converts_runner_configuration_errors_to_environment_code(
    tmp_path: Path, error: Exception
) -> None:
    class _BrokenRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            raise error

    diagnostics: list[str] = []

    codes = smt_cli.refresh_authoritative_state(
        tmp_path, _BrokenRunner([]), 60, diagnostics=diagnostics
    )

    assert codes == [smt_cli.EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE]
    assert any(str(error) in line for line in diagnostics)


@pytest.mark.parametrize(
    ("process_result", "expected"),
    [
        (ProcessResult(1, ("late",), timed_out=True), smt_cli.EXIT_TIMEOUT),
        (ProcessResult(1, ("break",), interrupted=True), smt_cli.EXIT_INTERRUPTED),
    ],
)
def test_refresh_uses_process_status_flags_for_public_internal_code(
    tmp_path: Path, process_result: ProcessResult, expected: int
) -> None:
    class _StatusRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            return process_result

    diagnostics: list[str] = []
    codes = smt_cli.refresh_authoritative_state(
        tmp_path, _StatusRunner([]), 60, diagnostics=diagnostics
    )

    assert codes == [expected]
    assert any(str(expected) in line for line in diagnostics)


def test_select_exact_safe_task_ignores_other_mod_and_ineligible_rows() -> None:
    tasks = [
        _task("00-other", mod="OtherMod"),
        _task("01-gui", resource_locks=["gui:desktop"]),
        _task("02-capability", required_agent_capability="gui:desktop"),
        _task("02-handoff", handoff_target="codex"),
        _task("03-high", risk="high"),
        _task("04-manual", executable=False, status="pending_manual"),
        _task("05-missing-dep", dependencies=["absent"]),
        _task("finished", status="done"),
        _task("06-busy", resource_locks=["resource:ExampleMod:X"]),
        _task(
            "busy-holder",
            status="running",
            resource_locks=["resource:ExampleMod:X"],
            lease_until="2999-01-01 00:00:00",
        ),
        _task("07-current", resource_locks=["resource:ExampleMod:Y"]),
    ]

    selected = smt_cli.select_exact_safe_task(
        _snapshot(tasks=tasks), "ExampleMod", datetime(2026, 7, 22, 12, 0, 0)
    )

    assert selected is not None
    assert selected["task_id"] == "07-current"


def test_executable_low_codex_handoff_is_never_auto_selected() -> None:
    handoff = _task("handoff", handoff_target="codex")
    snapshot = _snapshot(tasks=[handoff])

    assert smt_cli.select_exact_safe_task(
        snapshot, "ExampleMod", datetime(2026, 7, 22, 12, 0, 0)
    ) is None
    assert smt_cli.classify_outcome(snapshot, "ExampleMod", None) == "needs_gui"


@pytest.mark.parametrize(
    "malformed",
    [
        _task("bad-resources", resource_locks="mod:ExampleMod"),
        _task("bad-dependencies", dependencies="done"),
        _task("unknown-capability", required_agent_capability="agent:unknown"),
        _task("non-string-capability", required_agent_capability=7),
        _task("unknown-handoff", handoff_target="somewhere"),
    ],
)
def test_malformed_or_unknown_task_schema_is_never_auto_selected(
    malformed: dict[str, object],
) -> None:
    assert smt_cli.select_exact_safe_task(
        _snapshot(tasks=[malformed]),
        "ExampleMod",
        datetime(2026, 7, 22, 12, 0, 0),
    ) is None


@pytest.mark.parametrize(
    "malformed",
    [
        _task(
            "running-string-locks",
            status="running",
            lease_until="2999-01-01 00:00:00",
            resource_locks="resource:ExampleMod:shared",
        ),
        {**_task("empty-locks"), "resource_locks": []},
        _task("wrong-mod-scope", resource_locks=["mod:OtherMod"]),
        _task("unknown-capability-row", required_agent_capability="agent:unknown"),
        "not-a-task-object",
        {
            key: value
            for key, value in _task("missing-dependencies").items()
            if key != "dependencies"
        },
    ],
)
def test_any_invalid_task_row_fails_the_whole_snapshot_closed(
    malformed: object,
) -> None:
    snapshot = _snapshot(tasks=[])
    snapshot.workflow_tasks["tasks"] = [
        _task("otherwise-safe", resource_locks=["resource:ExampleMod:shared"]),
        malformed,
    ]

    selected = smt_cli.select_exact_safe_task(
        snapshot,
        "ExampleMod",
        datetime(2026, 7, 22, 12, 0, 0),
    )

    assert selected is None
    assert smt_cli.classify_outcome(snapshot, "ExampleMod", selected) == "blocked"


def test_duplicate_task_ids_fail_closed() -> None:
    snapshot = _snapshot(tasks=[_task("same"), _task("same")])

    assert smt_cli.select_exact_safe_task(
        snapshot,
        "ExampleMod",
        datetime(2026, 7, 22, 12, 0, 0),
    ) is None
    assert smt_cli.classify_outcome(snapshot, "ExampleMod", None) == "blocked"


def test_expired_running_task_is_recoverable_but_active_lease_is_not() -> None:
    now = datetime(2026, 7, 22, 12, 0, 0)
    expired = _task(
        "expired",
        status="running",
        lease_until="2026-07-22 11:59:59",
        resource_locks=["resource:ExampleMod:expired"],
    )
    active = _task(
        "active",
        status="running",
        lease_until="2026-07-22 12:00:01",
        resource_locks=["resource:ExampleMod:active"],
    )

    selected = smt_cli.select_exact_safe_task(
        _snapshot(tasks=[active, expired]), "ExampleMod", now
    )

    assert selected is not None
    assert selected["task_id"] == "expired"


@pytest.mark.parametrize(
    ("snapshot", "selected", "expected"),
    [
        (
            _snapshot(
                project_state="manual_tested",
                rows=[_state_row(state="manual_tested")],
            ),
            None,
            "completed",
        ),
        (
            _snapshot(
                project_state="ready_for_manual_test",
                rows=[_state_row(state="ready_for_manual_test")],
            ),
            None,
            "ready_for_manual_test",
        ),
        (_snapshot(tasks=[_task("safe")]), _task("safe"), None),
        (
            _snapshot(tasks=[_task("gui", executable=False, status="pending_manual", resource_locks=["gui:desktop"])]),
            None,
            "needs_gui",
        ),
        (
            _snapshot(rows=[_state_row(state="candidates_extracted")], tasks=[_task("translate", kind="agent_translation", executable=False, status="pending_manual")]),
            None,
            "needs_agent_translation",
        ),
        (
            _snapshot(project_state="needs_input", rows=[_state_row(state="needs_input")], tasks=[_task("choose", kind="needs_input", executable=False, status="pending_manual")]),
            None,
            "needs_user_input",
        ),
        (
            _snapshot(project_state="qa_failed", rows=[_state_row(state="qa_failed", blockers=["strict_gate_failed"])]),
            None,
            "blocked",
        ),
    ],
)
def test_classify_outcome_priority(
    snapshot: smt_cli.WorkflowSnapshot,
    selected: dict[str, object] | None,
    expected: smt_cli.PublicOutcome | None,
) -> None:
    assert smt_cli.classify_outcome(snapshot, "ExampleMod", selected) == expected


def test_classify_does_not_report_ready_with_project_or_other_mod_blocker() -> None:
    current = _state_row(state="ready_for_manual_test")
    other = _state_row(mod="OtherMod", state="blocked", blockers=["extra_mod_input"])

    assert smt_cli.classify_outcome(
        _snapshot(
            project_state="ready_for_manual_test",
            rows=[current, other],
            project_blockers=["project_input_conflict"],
        ),
        "ExampleMod",
        None,
    ) == "needs_user_input"


def test_safe_task_wins_over_later_gui_and_agent_actions() -> None:
    safe = _task("safe")
    snapshot = _snapshot(
        tasks=[
            _task("gui", executable=False, status="pending_manual", resource_locks=["gui:desktop"]),
            _task("translate", kind="agent_translation", executable=False, status="pending_manual"),
            safe,
        ]
    )

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", safe) is None


def test_classify_reads_gui_handoff_from_authoritative_next_actions() -> None:
    snapshot = _snapshot(
        rows=[
            _state_row(
                next_actions=[
                    {
                        "type": "run_command",
                        "reason": "controlled_writeback",
                        "required_agent_capability": "gui:desktop",
                        "handoff_target": "codex",
                        "risk": "low",
                    }
                ]
            )
        ]
    )

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", None) == "needs_gui"


def test_explicit_high_risk_action_stops_before_an_automatic_task() -> None:
    safe = _task("safe")
    snapshot = _snapshot(
        rows=[
            _state_row(
                next_actions=[
                    {
                        "type": "binary_writeback",
                        "reason": "high_risk_binary_writeback",
                        "risk": "high",
                    }
                ]
            )
        ],
        tasks=[safe],
    )

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", safe) == "blocked"


def test_recoverable_current_blocker_does_not_preempt_its_safe_repair_task() -> None:
    safe = _task("repair", kind="repair_candidate", reason="chs_package_missing")
    snapshot = _snapshot(
        project_state="blocked",
        rows=[_state_row(state="blocked", blockers=["chs_package_missing"])],
        tasks=[safe],
    )
    snapshot.policy["agent_orchestration_policy"]["auto_repair_allowed"] = [  # type: ignore[index]
        "chs_package_missing"
    ]

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", safe) is None


def test_must_stop_blocker_cannot_be_made_recoverable_by_task_reason() -> None:
    task = _task(
        "unsafe-repair",
        kind="repair_candidate",
        reason="unverified_plugin_output",
    )
    snapshot = _snapshot(
        project_state="blocked",
        rows=[_state_row(state="blocked", blockers=["unverified_plugin_output"])],
        tasks=[task],
    )
    snapshot.policy["agent_orchestration_policy"].update(  # type: ignore[index]
        {
            "auto_repair_allowed": ["unverified_plugin_output"],
            "must_stop_or_model_review": ["unverified_plugin_output"],
        }
    )

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", task) == "blocked"


@pytest.mark.parametrize(
    "blockers",
    [
        ["chs_package_missing", "provenance_missing"],
        ["provenance_missing", "chs_package_missing"],
    ],
)
def test_all_policy_allowed_current_blockers_can_continue_with_exact_task_identity(
    blockers: list[str],
) -> None:
    task = _task("repair", reason="chs_package_missing")
    snapshot = _snapshot(
        project_state="blocked",
        rows=[_state_row(state="blocked", blockers=blockers)],
        tasks=[task],
    )
    snapshot.policy["agent_orchestration_policy"]["auto_repair_allowed"] = blockers  # type: ignore[index]

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", task) is None


@pytest.mark.parametrize(
    "blockers",
    [
        ["chs_package_missing", "provenance_missing"],
        ["provenance_missing", "chs_package_missing"],
    ],
)
def test_any_non_recoverable_current_blocker_preempts_exact_safe_task(
    blockers: list[str],
) -> None:
    task = _task("repair", reason="chs_package_missing")
    snapshot = _snapshot(
        project_state="blocked",
        rows=[_state_row(state="blocked", blockers=blockers)],
        tasks=[task],
    )
    snapshot.policy["agent_orchestration_policy"]["auto_repair_allowed"] = [  # type: ignore[index]
        "chs_package_missing"
    ]

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", task) == "blocked"


def test_global_blocker_still_preempts_a_current_mod_safe_task() -> None:
    safe = _task("safe")
    snapshot = _snapshot(
        project_state="blocked",
        rows=[
            _state_row(),
            _state_row(mod="OtherMod", state="needs_input", blockers=["extra_mod_input"]),
        ],
        tasks=[safe],
    )

    assert smt_cli.classify_outcome(snapshot, "ExampleMod", safe) == "needs_user_input"


def test_state_digest_is_stable_and_ignores_generic_retry_count() -> None:
    first = _snapshot(tasks=[_task("task-b"), _task("task-a", status="failed")])
    second = _snapshot(tasks=[_task("task-a", status="failed"), _task("task-b")])
    second.workflow_state["states"][0]["retry_count"] = 0  # type: ignore[index]

    assert smt_cli.state_digest(first, "ExampleMod") == smt_cli.state_digest(second, "ExampleMod")


def _write_snapshot_files(
    workspace: Path,
    *,
    state: dict[str, object],
    tasks: dict[str, object],
    card: str = "# [SMT 进度]\n\n原始进度卡\n",
) -> smt_cli.SmtSession:
    state.setdefault("game_id", "skyrim-se")
    state.setdefault("game_profile_version", 2)
    state.setdefault("game_display_name", "Skyrim Special Edition")
    state.setdefault("support_level", "stable")
    state.setdefault("interface_translation_encoding", "utf-16-le-bom")
    state.setdefault("policy_path", "config/workflow_policy.json")
    state.setdefault("policy_sha256", "0" * 64)
    for row in state.get("states", []):
        if not isinstance(row, dict):
            continue
        row.setdefault("last_success_stage", str(row.get("state", "")))
        row.setdefault("blocking_issues", [])
        row.setdefault("allowed_scripts", [])
        row.setdefault("required_files", [])
        row.setdefault("recommended_actions", [])
        row.setdefault("repair_candidates", [])
    tasks.setdefault("generated_at", "2026-07-22 12:34:56")
    session = _session()
    (workspace / "qa").mkdir(parents=True)
    (workspace / ".workflow").mkdir(parents=True)
    (workspace / smt_cli.WORKSPACE_MARKER).write_text(
        json.dumps({"schema_version": 2, "kind": smt_cli.WORKSPACE_KIND, "game_id": "skyrim-se"}),
        encoding="utf-8",
    )
    (workspace / smt_cli.SESSION_RELATIVE_PATH).write_text(
        json.dumps(session.to_payload()), encoding="utf-8"
    )
    (workspace / "qa" / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")
    (workspace / "qa" / "workflow_tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
    (workspace / ".workflow" / "progress_card.md").write_text(card, encoding="utf-8")
    return session


def test_read_workflow_snapshot_reads_exact_authoritative_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [_state_row()]}
    tasks = {"schema_version": 1, "tasks": []}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks, card="ORIGINAL\n")
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")

    snapshot = smt_cli.read_workflow_snapshot(workspace, expected_session=session, policy_path=policy_path)

    assert snapshot.workflow_state == state
    assert snapshot.workflow_tasks == tasks
    assert snapshot.progress_card == "ORIGINAL\n"
    assert snapshot.marker["game_id"] == "skyrim-se"
    assert snapshot.session == session


@pytest.mark.parametrize("timeout_seconds", [60, 60.0])
def test_advance_uses_exact_resume_argv_and_stops_on_no_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: int | float,
) -> None:
    workspace = tmp_path / "workspace"
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [_state_row()]}
    tasks = {"schema_version": 1, "tasks": [_task("task-42")]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS) + [0])
    services = smt_cli.SmtServices(runner=runner, policy_path=policy_path, max_steps=4)

    result = smt_cli.advance_workflow(
        workspace,
        session,
        services,
        timeout_seconds,
    )

    resume_call = runner.calls[len(CORE_REFRESH_STEPS)]
    assert list(resume_call["argv"])[2:] == [  # type: ignore[arg-type]
        "--mode", "safe", "--mod-name", "ExampleMod", "--task-id", "task-42",
        "--include-serial", "--timeout-seconds", "60",
    ]
    assert Path(list(resume_call["argv"])[1]).name == "resume_workflow.py"  # type: ignore[arg-type]
    assert all("run_workflow_tasks.py" not in str(call["argv"]) for call in runner.calls)
    assert result.outcome == "blocked"
    assert result.exit_code == smt_cli.EXIT_SAFE_STOP
    assert "task-42" in " ".join(result.diagnostics)
    assert result.progress_card == "# [SMT 进度]\n\n原始进度卡\n"
    attempt_rows = [
        json.loads(line)
        for line in (workspace / "qa" / "workflow_agent_runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["status"] for row in attempt_rows] == ["started", "blocked"]
    assert all(row["task_id"] == "task-42" for row in attempt_rows)
    assert all("state_digest" in row and "blocker" in row for row in attempt_rows)

    _, last_attempt = write_workflow_state.agent_attempt_summary(
        workspace, "ExampleMod"
    )
    state["states"][0]["last_attempt"] = last_attempt  # type: ignore[index]
    (workspace / "qa" / "workflow_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    second_runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))

    second_result = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(
            runner=second_runner,
            policy_path=policy_path,
            max_steps=4,
        ),
        60,
    )

    assert not any(
        "resume_workflow.py" in str(call["argv"]) for call in second_runner.calls
    )
    assert second_result.outcome == "blocked"
    assert second_result.exit_code == smt_cli.EXIT_SAFE_STOP
    assert any("last_attempt" in item for item in second_result.diagnostics)


def test_advance_does_not_trust_last_attempt_without_digest_and_blocker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task = _task("task-42", evidence="qa/evidence.json")
    row = _state_row(
        last_attempt={
            "state": "candidates_extracted",
            "action": "python scripts/example.py",
            "evidence": "qa/evidence.json",
            "status": "failed",
        }
    )
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [row]}
    tasks = {"schema_version": 1, "tasks": [task]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))

    result = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(runner=runner, policy_path=policy_path, max_steps=4),
        60,
    )

    assert sum(
        "resume_workflow.py" in str(call["argv"]) for call in runner.calls
    ) == 1
    assert result.outcome == "blocked"
    assert result.exit_code == smt_cli.EXIT_SAFE_STOP
    assert not any("last_attempt" in item for item in result.diagnostics)


def test_advance_stops_on_fully_proven_unchanged_last_attempt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task = _task("task-42", evidence="qa/evidence.json")
    row = _state_row()
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [row]}
    tasks = {"schema_version": 1, "tasks": [task]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    snapshot = smt_cli.read_workflow_snapshot(workspace, expected_session=session, policy_path=policy_path)
    row["last_attempt"] = {
        "command": "python scripts/example.py",
        "evidence": "qa/evidence.json",
        "status": "failed",
        "state_digest": smt_cli.state_digest(snapshot, "ExampleMod"),
        "blocker": "",
    }
    (workspace / "qa" / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))

    result = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(runner=runner, policy_path=policy_path),
        60,
    )

    assert len(runner.calls) == len(CORE_REFRESH_STEPS)
    assert result.outcome == "blocked"
    assert any("last_attempt" in item for item in result.diagnostics)


def test_real_agent_log_preserves_complete_attempt_proof_for_state_projection(
    tmp_path: Path,
) -> None:
    (tmp_path / "qa").mkdir()
    task = _task("task-42", evidence="qa/evidence.json")
    snapshot = _snapshot(tasks=[task])
    digest = smt_cli.state_digest(snapshot, "ExampleMod")

    workflow_agent_log.append_workflow_agent_event(
        root=tmp_path,
        mod_name="ExampleMod",
        state="candidates_extracted",
        event="smt_command",
        action="python scripts/example.py",
        command="python scripts/example.py",
        status="failed",
        evidence="qa/evidence.json",
        task_id="task-42",
        state_digest=digest,
        blocker="",
    )
    _, last_attempt = write_workflow_state.agent_attempt_summary(
        tmp_path, "ExampleMod"
    )
    projected = _snapshot(
        rows=[_state_row(last_attempt=last_attempt)], tasks=[task]
    )

    assert last_attempt["task_id"] == "task-42"
    assert last_attempt["command"] == "python scripts/example.py"
    assert last_attempt["state_digest"] == digest
    assert "blocker" in last_attempt
    assert smt_cli._cross_command_attempt_unchanged(  # noqa: SLF001
        projected, "ExampleMod", task, smt_cli.state_digest(projected, "ExampleMod")
    )


def test_smt_proof_row_does_not_double_count_underlying_retry(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    rows = [
        {
            "mod": "ExampleMod",
            "event": "command",
            "status": "failed",
            "timestamp": "1",
        },
        {
            "mod": "ExampleMod",
            "event": "smt_command",
            "status": "failed",
            "timestamp": "2",
            "command": "python scripts/example.py",
            "evidence": "qa/evidence.json",
            "task_id": "task-42",
            "state_digest": "1" * 64,
            "blocker": "",
        },
    ]
    (tmp_path / "qa" / "workflow_agent_runs.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    retry_count, last_attempt = write_workflow_state.agent_attempt_summary(
        tmp_path, "ExampleMod"
    )

    assert retry_count == 0
    assert last_attempt["event"] == "smt_command"
    assert last_attempt["state_digest"] == "1" * 64


def test_changed_last_attempt_evidence_does_not_suppress_current_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task = _task("task-42", evidence="qa/current.json")
    row = _state_row(
        last_attempt={
            "command": "python scripts/example.py",
            "evidence": "qa/old.json",
            "status": "failed",
            "state_digest": "f" * 64,
            "blocker": "",
        }
    )
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [row]}
    tasks = {"schema_version": 1, "tasks": [task]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS) + [0])

    smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(runner=runner, policy_path=policy_path),
        60,
    )

    assert sum(
        "resume_workflow.py" in str(call["argv"]) for call in runner.calls
    ) == 1


def test_advance_limits_same_blocker_and_evidence_to_policy_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _snapshot(tasks=[_task("task-1", evidence="qa/same.json")])
    second = _snapshot(tasks=[_task("task-2", evidence="qa/same.json")])
    third = _snapshot(tasks=[_task("task-3", evidence="qa/same.json")])
    snapshots = [first, second, second, third, third]
    runner = _RecordingRunner([0] * 20)
    monkeypatch.setattr(smt_cli, "refresh_authoritative_state", lambda *args, **kwargs: [0, 0, 0, 0])
    monkeypatch.setattr(smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: snapshots.pop(0))

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=runner, max_steps=5, attempt_logger=_discard_attempt
        ),
        60,
    )

    resume_calls = [
        call for call in runner.calls if "resume_workflow.py" in str(call["argv"])
    ]
    assert len(resume_calls) == 2
    assert result.outcome == "blocked"
    assert any("policy attempt limit" in item for item in result.diagnostics)


def test_internal_resume_no_task_code_becomes_public_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(tasks=[_task("task-1")])
    after = _snapshot(
        project_state="packaged",
        rows=[_state_row(state="packaged")],
        tasks=[],
    )
    snapshots = [before, after]

    class _NoTaskRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            return ProcessResult(2, ("No eligible safe workflow task found.",))

    refresh_calls: list[object] = []
    monkeypatch.setattr(
        smt_cli,
        "refresh_authoritative_state",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or [0, 0, 0, 0],
    )
    monkeypatch.setattr(smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: snapshots.pop(0))

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=_NoTaskRunner([]),
            max_steps=2,
            attempt_logger=_discard_attempt,
        ),
        60,
    )

    assert result.outcome is None
    assert result.exit_code == smt_cli.EXIT_SUCCESS
    assert 2 in result.underlying_exit_codes
    assert len(refresh_calls) == 2


def test_exit_two_no_task_attempt_does_not_suppress_expired_lease_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "qa").mkdir(parents=True)
    pending = _snapshot(tasks=[_task("task-1")])
    active_lease = _snapshot(
        tasks=[
            _task(
                "task-1",
                status="running",
                lease_until="2999-01-01 00:00:00",
            )
        ]
    )
    snapshots = [pending, active_lease]
    runner = _RecordingRunner([2, 0])
    monkeypatch.setattr(
        smt_cli,
        "refresh_authoritative_state",
        lambda *args, **kwargs: [0, 0, 0, 0],
    )
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: snapshots.pop(0),
    )

    first_result = smt_cli.advance_workflow(
        workspace,
        _session(),
        smt_cli.SmtServices(runner=runner, max_steps=2),
        60,
    )

    first_rows = [
        json.loads(line)
        for line in (workspace / "qa" / "workflow_agent_runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["status"] for row in first_rows] == ["started", "skipped"]
    assert "no_task" in first_rows[-1]["details"]
    assert 2 in first_result.underlying_exit_codes
    _, last_attempt = write_workflow_state.agent_attempt_summary(
        workspace, "ExampleMod"
    )
    assert last_attempt["status"] == "skipped"

    expired_lease = _snapshot(
        rows=[_state_row(last_attempt=last_attempt)],
        tasks=[
            _task(
                "task-1",
                status="running",
                lease_until="2000-01-01 00:00:00",
            )
        ],
    )
    after_retry = _snapshot(
        tasks=[
            _task(
                "gui",
                executable=False,
                status="pending_manual",
                resource_locks=["gui:desktop"],
            )
        ]
    )
    snapshots.extend([expired_lease, after_retry])

    second_result = smt_cli.advance_workflow(
        workspace,
        _session(),
        smt_cli.SmtServices(runner=runner, max_steps=2),
        60,
    )

    assert len(runner.calls) == 2
    assert second_result.outcome == "needs_gui"
    all_rows = [
        json.loads(line)
        for line in (workspace / "qa" / "workflow_agent_runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["status"] for row in all_rows] == [
        "started",
        "skipped",
        "started",
        "passed",
    ]


def test_internal_resume_two_refreshes_then_returns_new_gui_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(tasks=[_task("task-1")])
    after = _snapshot(
        tasks=[
            _task(
                "gui",
                executable=False,
                status="pending_manual",
                resource_locks=["gui:desktop"],
            )
        ]
    )
    after = smt_cli.WorkflowSnapshot(
        workspace=after.workspace,
        marker=after.marker,
        session=after.session,
        workflow_state=after.workflow_state,
        workflow_tasks=after.workflow_tasks,
        progress_card="NEW GUI CARD\n",
        policy=after.policy,
    )
    snapshots = [before, after]
    refresh_calls: list[object] = []

    class _NoTaskRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            return ProcessResult(2, ())

    monkeypatch.setattr(
        smt_cli,
        "refresh_authoritative_state",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or [0, 0, 0, 0],
    )
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: snapshots.pop(0),
    )

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=_NoTaskRunner([]),
            max_steps=2,
            attempt_logger=_discard_attempt,
        ),
        60,
    )

    assert len(refresh_calls) == 2
    assert result.outcome == "needs_gui"
    assert result.exit_code == smt_cli.EXIT_SAFE_STOP
    assert result.progress_card == "NEW GUI CARD\n"


def test_successful_resume_also_refreshes_before_using_new_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(tasks=[_task("task-1")])
    after = _snapshot(
        tasks=[
            _task(
                "gui",
                executable=False,
                status="pending_manual",
                resource_locks=["gui:desktop"],
            )
        ]
    )
    snapshots = [before, after]
    refresh_calls: list[object] = []
    monkeypatch.setattr(
        smt_cli,
        "refresh_authoritative_state",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or [0, 0, 0, 0],
    )
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: snapshots.pop(0),
    )

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=_RecordingRunner([0]),
            max_steps=2,
            attempt_logger=_discard_attempt,
        ),
        60,
    )

    assert len(refresh_calls) == 2
    assert result.outcome == "needs_gui"


def test_resume_runner_value_error_returns_environment_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tasks=[_task("task-1")])

    class _BrokenRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            raise ValueError("invalid output codec")

    monkeypatch.setattr(
        smt_cli, "refresh_authoritative_state", lambda *args, **kwargs: [0, 0, 0, 0]
    )
    monkeypatch.setattr(
        smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: snapshot
    )

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=_BrokenRunner([]), attempt_logger=_discard_attempt
        ),
        60,
    )

    assert result.exit_code == smt_cli.EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
    assert any("invalid output codec" in line for line in result.diagnostics)


def test_advance_maps_profile_capability_block_to_exit_four(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = {
        "schema_version": 1,
        "generated_at": "local-time",
        "project_state": "blocked",
        "states": [_state_row(state="blocked", blockers=["capability:plugin_text:unsupported"])],
    }
    tasks = {"schema_version": 1, "tasks": []}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))

    result = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(runner=runner, policy_path=policy_path),
        60,
    )

    assert result.outcome == "blocked"
    assert result.exit_code == smt_cli.EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY


def test_advance_observes_total_deadline_after_refresh(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [_state_row()]}
    tasks = {"schema_version": 1, "tasks": [_task("task-42")]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    moments = iter([0.0, 0.0, 61.0])

    result = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(
            runner=_RecordingRunner([0] * len(CORE_REFRESH_STEPS)),
            policy_path=policy_path,
            monotonic=lambda: next(moments),
        ),
        60,
    )

    assert result.exit_code == smt_cli.EXIT_TIMEOUT


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (ProcessResult(124, (), timed_out=True), smt_cli.EXIT_TIMEOUT),
        (ProcessResult(130, (), interrupted=True), smt_cli.EXIT_INTERRUPTED),
    ],
)
def test_advance_maps_timeout_and_interrupt(
    tmp_path: Path, result: ProcessResult, expected_code: int
) -> None:
    workspace = tmp_path / "workspace"
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [_state_row()]}
    tasks = {"schema_version": 1, "tasks": [_task("task-42")]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")

    class _Runner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            if len(self.calls) <= len(CORE_REFRESH_STEPS):
                return ProcessResult(0, ())
            return result

    outcome = smt_cli.advance_workflow(
        workspace,
        session,
        smt_cli.SmtServices(runner=_Runner([]), policy_path=policy_path, max_steps=4),
        60,
    )

    assert outcome.exit_code == expected_code


def test_advance_stops_at_max_steps_even_when_each_task_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = [
        _snapshot(tasks=[_task("task-1")]),
        _snapshot(tasks=[_task("task-2")]),
        _snapshot(tasks=[_task("task-3")]),
        _snapshot(tasks=[_task("task-4")]),
    ]
    runner = _RecordingRunner([0] * 20)
    monkeypatch.setattr(smt_cli, "refresh_authoritative_state", lambda *args, **kwargs: [0, 0, 0, 0])
    monkeypatch.setattr(smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: snapshots.pop(0))

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=runner, max_steps=2, attempt_logger=_discard_attempt
        ),
        60,
    )

    assert result.outcome == "blocked"
    assert result.exit_code == smt_cli.EXIT_SAFE_STOP
    assert any("maximum" in item.lower() for item in result.diagnostics)


def test_advance_diagnostics_remain_bounded_across_multiple_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [
        _snapshot(tasks=[_task("task-1")]),
        _snapshot(tasks=[_task("task-2")]),
        _snapshot(tasks=[_task("task-3")]),
        _snapshot(tasks=[_task("task-4")]),
    ]

    class _LargeTailRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            call_number = len(self.calls)
            return ProcessResult(
                0,
                tuple(
                    f"call-{call_number}-line-{line_number}"
                    for line_number in range(201)
                ),
            )

    runner = _LargeTailRunner([])
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: snapshots.pop(0),
    )

    result = smt_cli.advance_workflow(
        Path(r"D:\SMT\Example"),
        _session(),
        smt_cli.SmtServices(
            runner=runner,
            max_steps=2,
            attempt_logger=_discard_attempt,
        ),
        60,
    )

    assert len(result.diagnostics) == 200
    assert result.diagnostics[-1] == "maximum workflow steps reached"
    assert result.diagnostics[-2].endswith(
        f"call-{len(runner.calls)}-line-200"
    )


class _ImmediateLock:
    def acquire(self) -> "_ImmediateLock":
        return self

    def release(self) -> None:
        return None

    def __enter__(self) -> "_ImmediateLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


@pytest.mark.parametrize("tool_setup", ["skip", "auto"])
def test_run_new_workspace_uses_init_prepare_refresh_then_advance(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_setup: str,
) -> None:
    tmp_path = cli_safe_tmp_path
    source = tmp_path / "ExampleMod.zip"
    source.write_bytes(b"safe fixture")
    workspace = tmp_path / "workspaces" / "ExampleMod"

    class _RunRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            call = {"argv": list(argv), **kwargs}  # type: ignore[arg-type]
            self.calls.append(call)
            script = Path(call["argv"][1]).name  # type: ignore[index]
            if script == "init_workspace.py":
                workspace.mkdir(parents=True)
                (workspace / ".workflow").mkdir()
                (workspace / "mod").mkdir()
                (workspace / ".skyrim-chs-workspace.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "kind": smt_cli.WORKSPACE_KIND,
                            "game_id": "skyrim-se",
                        }
                    ),
                    encoding="utf-8",
                )
            return ProcessResult(exit_code=0, output_tail=(script,))

    runner = _RunRunner([])
    advance_calls: list[tuple[Path, smt_cli.SmtSession]] = []

    def fake_advance(
        selected_workspace: Path,
        session: smt_cli.SmtSession,
        services: smt_cli.SmtServices,
        timeout_seconds: int | float,
    ) -> smt_cli.CliResult:
        del services, timeout_seconds
        advance_calls.append((selected_workspace, session))
        return smt_cli.CliResult(
            command="resume",
            outcome="needs_agent_translation",
            exit_code=smt_cli.EXIT_SAFE_STOP,
            workspace=str(selected_workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
        )

    monkeypatch.setattr(smt_cli, "advance_workflow", fake_advance)

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=tmp_path / "state",
            tool_setup=tool_setup,  # type: ignore[arg-type]
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    scripts = [Path(call["argv"][1]).name for call in runner.calls]  # type: ignore[index]
    assert scripts == [
        "init_workspace.py",
        "run_translation_queue.py",
        *(step.script for step in CORE_REFRESH_STEPS),
    ]
    assert list(runner.calls[0]["argv"])[2:] == [  # type: ignore[arg-type]
        str(workspace),
        "--game",
        "skyrim-se",
        "--tool-setup",
        tool_setup,
    ]
    assert runner.calls[0]["cwd"] == REPOSITORY_ROOT
    assert runner.calls[0]["env"]["SKYRIM_CHS_WORKSPACE_ROOT"] == str(workspace)  # type: ignore[index]
    queue_argv = list(runner.calls[1]["argv"])  # type: ignore[arg-type]
    assert queue_argv[2:] == [
        "--mode",
        "prepare",
        "--mod-name",
        "ExampleMod",
        "--source-path",
        "mod/ExampleMod.zip",
        "--limit",
        "1",
    ]
    assert "workflow" not in queue_argv
    assert "setup_workspace_tools.py" not in scripts
    assert advance_calls and advance_calls[0][0] == workspace
    assert result.command == "run"
    assert result.outcome == "needs_agent_translation"


def test_resume_resolves_explicit_workspace_and_delegates_to_same_advance(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = cli_safe_tmp_path
    workspace = tmp_path / "workspace"
    session = _session()
    (workspace / ".workflow").mkdir(parents=True)
    (workspace / "mod").mkdir()
    (workspace / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": smt_cli.WORKSPACE_KIND,
                "game_id": session.game_id,
            }
        ),
        encoding="utf-8",
    )
    imported = workspace / session.import_relative_path
    imported.write_bytes(b"fixture")
    digest = __import__("hashlib").sha256(b"fixture").hexdigest()
    session = smt_cli.SmtSession(
        **{
            **session.to_payload(),
            "source_sha256": digest,
            "imported_sha256": digest,
            "input_identity": f"smt-input-v1:skyrim-se:zip:{digest}",
        }
    )
    smt_cli.create_session_no_replace(
        workspace / smt_cli.SESSION_RELATIVE_PATH,
        session,
    )
    observed: list[tuple[Path, smt_cli.SmtSession]] = []

    def fake_advance(
        selected_workspace: Path,
        selected_session: smt_cli.SmtSession,
        services: smt_cli.SmtServices,
        timeout_seconds: int | float,
    ) -> smt_cli.CliResult:
        del services, timeout_seconds
        observed.append((selected_workspace, selected_session))
        return smt_cli.CliResult(
            command="resume",
            exit_code=0,
            message="no-op",
            workspace=str(selected_workspace),
            mod_name=selected_session.mod_name,
            game_id=selected_session.game_id,
        )

    monkeypatch.setattr(smt_cli, "advance_workflow", fake_advance)
    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            workspace=workspace,
            local_state_root=tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=_RecordingRunner([])),
    )

    assert result.exit_code == 0
    assert result.command == "resume"
    assert observed == [(workspace, session)]


def _create_committed_zip_workspace(
    root: Path,
) -> tuple[Path, Path, smt_cli.SmtSession, smt_cli.CliStateStore]:
    source = root / "ExampleMod.zip"
    source.write_bytes(b"same immutable archive")
    digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    workspace = root / "workspaces" / "ExampleMod"
    (workspace / ".workflow").mkdir(parents=True)
    (workspace / "mod").mkdir()
    (workspace / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": smt_cli.WORKSPACE_KIND,
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    (workspace / "mod" / "ExampleMod.zip").write_bytes(source.read_bytes())
    session = smt_cli.SmtSession(
        schema_version=1,
        workspace_id="00000000-0000-4000-8000-000000000099",
        mod_name="ExampleMod",
        game_id="skyrim-se",
        fingerprint_algorithm="smt-input-v1",
        input_identity=f"smt-input-v1:skyrim-se:zip:{digest}",
        source_kind="zip",
        source_display_name="ExampleMod.zip",
        source_sha256=digest,
        import_relative_path="mod/ExampleMod.zip",
        imported_sha256=digest,
        created_at="2026-07-22T00:00:00+00:00",
    )
    smt_cli.create_session_no_replace(
        workspace / smt_cli.SESSION_RELATIVE_PATH,
        session,
    )
    store = smt_cli.CliStateStore(root / "state")
    state = store.read()
    state["last_workspace"] = str(workspace)
    state["input_mappings"] = {session.input_identity: str(workspace)}
    store.write(state)
    return source, workspace, session, store


@pytest.mark.parametrize(
    ("tool_setup", "expected_setup_mode"),
    [("auto", "auto"), ("manual", "manual"), ("skip", None)],
)
def test_run_reused_workspace_applies_exact_tool_setup_policy_without_reimport(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_setup: str,
    expected_setup_mode: str | None,
) -> None:
    source, workspace, session, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )
    imported = workspace / session.import_relative_path
    session_path = workspace / smt_cli.SESSION_RELATIVE_PATH
    before = (imported.read_bytes(), imported.stat().st_mtime_ns, session_path.read_bytes())
    formal_steps: list[str] = []
    operation_held = [False]

    class _TrackedLock(_ImmediateLock):
        def __init__(self, operation: bool) -> None:
            self.operation = operation

        def acquire(self) -> "_TrackedLock":
            if self.operation:
                assert not operation_held[0]
                operation_held[0] = True
            return self

        def release(self) -> None:
            if self.operation:
                operation_held[0] = False

    def lock_factory(path: Path, *args: object, **kwargs: object) -> _TrackedLock:
        del args, kwargs
        return _TrackedLock(path.name == "smt-operation.lock")

    class _OrderRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            assert operation_held[0]
            script = Path(list(argv)[1]).name  # type: ignore[arg-type]
            if script == "setup_workspace_tools.py":
                formal_steps.append("tool")
            elif script == "run_translation_queue.py":
                formal_steps.append("queue")
            return super().run(argv, **kwargs)

    runner = _OrderRunner([0] * 20)
    real_import = smt_cli.import_input_transactionally

    def recording_import(*args: object, **kwargs: object) -> smt_cli.SmtSession:
        assert operation_held[0]
        formal_steps.append("revalidate")
        return real_import(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(smt_cli, "import_input_transactionally", recording_import)
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda selected_workspace, selected_session, services, timeout: smt_cli.CliResult(
            command="resume",
            exit_code=0,
            message="no-op",
            workspace=str(selected_workspace),
            mod_name=selected_session.mod_name,
            game_id=selected_session.game_id,
        ),
    )

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup=tool_setup,  # type: ignore[arg-type]
            lock_factory=lock_factory,
        ),
        smt_cli.SmtServices(runner=runner),
    )

    setup_calls = [
        list(call["argv"])
        for call in runner.calls
        if Path(list(call["argv"])[1]).name == "setup_workspace_tools.py"
    ]
    if expected_setup_mode is None:
        assert setup_calls == []
        assert formal_steps[:2] == ["revalidate", "queue"]
    else:
        assert len(setup_calls) == 1
        assert setup_calls[0][2:] == ["--mode", expected_setup_mode]
        assert formal_steps[:3] == ["tool", "revalidate", "queue"]
    assert not any(
        Path(list(call["argv"])[1]).name == "init_workspace.py"
        for call in runner.calls
    )
    assert (
        imported.read_bytes(),
        imported.stat().st_mtime_ns,
        session_path.read_bytes(),
    ) == before
    assert result.exit_code == 0
    assert not operation_held[0]


def test_reused_workspace_rechecks_source_after_tool_setup(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )

    class _MutatingSetupRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            script = Path(list(argv)[1]).name  # type: ignore[arg-type]
            result = super().run(argv, **kwargs)
            if script == "setup_workspace_tools.py":
                source.write_bytes(b"changed while tools were prepared")
            return result

    runner = _MutatingSetupRunner([0] * 20)
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: smt_cli.CliResult(command="resume", exit_code=0),
    )

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="auto",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.exit_code == 6
    assert (
        "input changed"
        in (result.message + " " + " ".join(result.diagnostics)).casefold()
    )
    assert not any(
        Path(list(call["argv"])[1]).name == "run_translation_queue.py"
        for call in runner.calls
    )


def test_bound_directory_entry_change_maps_to_identity_conflict(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cli_safe_tmp_path / "DirectoryMod"
    source.mkdir()
    source_file = source / "menu.txt"
    source_file.write_text("before", encoding="utf-8")
    workspace = cli_safe_tmp_path / "directory-workspace"

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        (target / ".workflow").mkdir(parents=True)
        (target / "mod").mkdir()
        (target / ".skyrim-chs-workspace.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": smt_cli.WORKSPACE_KIND,
                    "game_id": game_id,
                }
            ),
            encoding="utf-8",
        )

    def mutating_copier(source_path: Path, target_path: Path) -> None:
        __import__("shutil").copyfile(source_path, target_path)
        source_path.write_text("changed", encoding="utf-8")

    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("identity change must stop before advance")
        ),
    )
    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="skip",
            initializer=initializer,
            copier=mutating_copier,
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=_RecordingRunner([])),
    )

    assert result.exit_code == 6
    assert (
        "input changed"
        in (result.message + " " + " ".join(result.diagnostics)).casefold()
    )


def test_run_continues_exact_current_session_when_extra_mod_has_not_affected_state(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )
    (workspace / "mod" / "Unregistered.zip").write_bytes(b"other")
    runner = _RecordingRunner([0] * 20)
    monkeypatch.setattr(smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: _snapshot())
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda selected_workspace, selected_session, services, timeout: smt_cli.CliResult(
            command="resume",
            exit_code=0,
            message="continued current session",
            workspace=str(selected_workspace),
            mod_name=selected_session.mod_name,
            game_id=selected_session.game_id,
        ),
    )

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="auto",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.outcome is None
    assert result.exit_code == 0
    assert "mod/Unregistered.zip" in " ".join(result.diagnostics)
    assert "doctor" in " ".join(result.diagnostics).casefold()
    assert [Path(list(call["argv"])[1]).name for call in runner.calls] == [
        "setup_workspace_tools.py",
        "run_translation_queue.py",
        *(step.script for step in CORE_REFRESH_STEPS),
    ]


def test_run_stops_when_authoritative_state_contains_extra_mod_lane(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )
    (workspace / "mod" / "Unregistered.zip").write_bytes(b"other")
    runner = _RecordingRunner([0] * 20)
    contaminated = _snapshot(
        rows=[
            _state_row(),
            _state_row(
                mod="Unregistered",
                state="needs_input",
                blockers=["extra_mod_input"],
            ),
        ]
    )
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: contaminated,
    )
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("contaminated authoritative state must stop before advance")
        ),
    )

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="skip",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.outcome == "needs_user_input"
    assert result.exit_code == 3
    assert result.next_action is not None
    assert result.next_action["artifacts"] == ["mod/Unregistered.zip"]
    assert Path(list(runner.calls[0]["argv"])[1]).name == "run_translation_queue.py"


def test_extra_input_projection_treats_current_mod_lane_case_insensitively() -> None:
    snapshot = _snapshot(rows=[_state_row(mod="examplemod")])

    assert not smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/A.zip"],
    )


def test_extra_input_projection_does_not_match_bare_mod_name_substrings() -> None:
    snapshot = _snapshot(
        tasks=[
            _task(
                "current",
                command="python scripts/example.py --mod-name ExampleMod",
            )
        ]
    )

    assert not smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/Mod.zip"],
    )


def test_extra_input_projection_does_not_match_unrelated_evidence_basename() -> None:
    snapshot = _snapshot(tasks=[_task("current", evidence="qa/foo.json")])

    assert not smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/Foo.zip"],
    )


def test_extra_input_projection_does_not_match_natural_language_reason() -> None:
    snapshot = _snapshot(
        tasks=[
            _task(
                "current",
                reason="This command supports multiple Mod inputs when requested",
            )
        ]
    )

    assert not smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/Foo.zip"],
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(
            tasks=[
                _task(
                    "current",
                    command=(
                        "python scripts/example.py --source-path mod/Foo.zip"
                    ),
                )
            ]
        ),
        _snapshot(
            rows=[
                _state_row(
                    next_actions=[
                        {
                            "type": "needs_input",
                            "artifacts": ["mod/Foo.zip"],
                        }
                    ]
                )
            ]
        ),
    ],
)
def test_extra_input_projection_accepts_only_full_relative_path_reference(
    snapshot: smt_cli.WorkflowSnapshot,
) -> None:
    assert smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/Foo.zip"],
    )


def test_extra_input_projection_accepts_full_directory_relative_path() -> None:
    snapshot = _snapshot(
        rows=[
            _state_row(
                next_actions=[
                    {
                        "type": "needs_input",
                        "artifacts": ["mod/ExtraDirectory"],
                    }
                ]
            )
        ]
    )

    assert smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/ExtraDirectory"],
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(rows=[_state_row(blockers=["extra_mod"])]),
        _snapshot(tasks=[_task("current", reason="extra_mod")]),
    ],
)
def test_extra_input_projection_accepts_structured_reason_or_blocker_code(
    snapshot: smt_cli.WorkflowSnapshot,
) -> None:
    assert smt_cli.extra_inputs_affect_authoritative_state(
        snapshot,
        _session(),
        ["mod/Foo.zip"],
    )


def test_resume_uses_last_workspace_and_continues_unaffected_current_session(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )
    (workspace / "mod" / "Unregistered.zip").write_bytes(b"other")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))
    monkeypatch.setattr(smt_cli, "read_workflow_snapshot", lambda *args, **kwargs: _snapshot())
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda selected_workspace, selected_session, services, timeout: smt_cli.CliResult(
            command="resume",
            exit_code=0,
            message="continued current session",
            workspace=str(selected_workspace),
            mod_name=selected_session.mod_name,
            game_id=selected_session.game_id,
        ),
    )

    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            cwd=cli_safe_tmp_path,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.workspace == str(workspace)
    assert result.outcome is None
    assert result.exit_code == 0
    assert "doctor" in " ".join(result.diagnostics).casefold()
    assert [Path(call["argv"][1]).name for call in runner.calls] == [  # type: ignore[index]
        step.script for step in CORE_REFRESH_STEPS
    ]


def test_resume_stops_when_extra_input_is_named_by_authoritative_blocker(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )
    (workspace / "mod" / "Unregistered.zip").write_bytes(b"other")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS))
    contaminated = _snapshot(
        rows=[
            _state_row(
                blockers=["unregistered_mod_input"],
            )
        ]
    )
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: contaminated,
    )
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("contaminated authoritative state must stop before advance")
        ),
    )

    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.outcome == "needs_user_input"
    assert result.exit_code == 3
    assert result.next_action is not None
    assert result.next_action["artifacts"] == ["mod/Unregistered.zip"]


def test_resume_operation_lock_timeout_maps_to_workspace_conflict(
    cli_safe_tmp_path: Path,
) -> None:
    _source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )

    class _BusyLock(_ImmediateLock):
        def acquire(self) -> "_BusyLock":
            raise smt_cli.SmtLockTimeoutError("held")

    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _BusyLock(),
        )
    )

    assert result.exit_code == 6
    assert result.outcome is None
    assert "busy" in result.message


def test_resume_lock_release_failure_returns_environment_result(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )

    class _ReleaseFailLock(_ImmediateLock):
        def release(self) -> None:
            raise OSError(5, "release denied")

    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda selected_workspace, selected_session, services, timeout: (
            smt_cli.CliResult(
                command="resume",
                exit_code=0,
                workspace=str(selected_workspace),
                mod_name=selected_session.mod_name,
                game_id=selected_session.game_id,
            )
        ),
    )

    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _ReleaseFailLock(),
        )
    )

    assert result.exit_code == 5
    assert "release" in " ".join(result.diagnostics).casefold()


def test_resume_lock_release_keyboard_interrupt_returns_interrupted(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )

    class _ReleaseInterruptedLock(_ImmediateLock):
        def release(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda selected_workspace, selected_session, services, timeout: smt_cli.CliResult(
            command="resume",
            exit_code=0,
            workspace=str(selected_workspace),
            mod_name=selected_session.mod_name,
            game_id=selected_session.game_id,
        ),
    )

    result = smt_cli.resume_command(
        smt_cli.ResumeRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lambda *args, **kwargs: _ReleaseInterruptedLock(),
        )
    )

    assert result.exit_code == 130
    assert "KeyboardInterrupt" in " ".join(result.diagnostics)


def _run_with_reservation_release_exception(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: BaseException,
    released: list[str],
) -> smt_cli.CliResult:
    source = cli_safe_tmp_path / "ReleaseSignal.zip"
    source.write_bytes(b"fixture")
    workspace = cli_safe_tmp_path / "workspace"

    class _NamedLock(_ImmediateLock):
        def __init__(self, name: str) -> None:
            self.name = name

        def release(self) -> None:
            released.append(self.name)
            if self.name == "reservation":
                raise exception

    def lock_factory(path: Path, *args: object, **kwargs: object) -> _NamedLock:
        del args, kwargs
        if path.name == "smt-operation.lock":
            return _NamedLock("operation")
        if path.parent.name == "reservation-locks":
            return _NamedLock("reservation")
        return _NamedLock("global")

    class _InitRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            call = {"argv": list(argv), **kwargs}  # type: ignore[arg-type]
            self.calls.append(call)
            script = Path(call["argv"][1]).name  # type: ignore[index]
            if script == "init_workspace.py":
                target = Path(call["argv"][2])  # type: ignore[index]
                (target / ".workflow").mkdir(parents=True)
                (target / "mod").mkdir()
                (target / ".skyrim-chs-workspace.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "kind": smt_cli.WORKSPACE_KIND,
                            "game_id": "skyrim-se",
                        }
                    ),
                    encoding="utf-8",
                )
            return ProcessResult(0, (script,))

    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: smt_cli.CliResult(command="run", exit_code=0),
    )
    return smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="skip",
            lock_factory=lock_factory,
        ),
        smt_cli.SmtServices(runner=_InitRunner([])),
    )


def test_run_release_keyboard_interrupt_releases_operation_and_returns_interrupted(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    result = _run_with_reservation_release_exception(
        cli_safe_tmp_path,
        monkeypatch,
        KeyboardInterrupt(),
        released,
    )

    assert result.exit_code == 130
    assert released.count("reservation") == 1
    assert released.count("operation") == 1


def test_run_release_system_exit_releases_operation_and_reraises(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    with pytest.raises(SystemExit, match="release exit"):
        _run_with_reservation_release_exception(
            cli_safe_tmp_path,
            monkeypatch,
            SystemExit("release exit"),
            released,
        )

    assert released.count("reservation") == 1
    assert released.count("operation") == 1


def test_workspace_resolution_close_releases_both_locks_before_interrupt(
    cli_safe_tmp_path: Path,
) -> None:
    source = cli_safe_tmp_path / "CloseSignal.zip"
    source.write_bytes(b"fixture")
    released: list[str] = []

    class _NamedLock(_ImmediateLock):
        def __init__(self, name: str) -> None:
            self.name = name

        def release(self) -> None:
            released.append(self.name)
            if self.name == "workspace":
                raise KeyboardInterrupt

    def lock_factory(path: Path, *args: object, **kwargs: object) -> _NamedLock:
        del args, kwargs
        if path.parent.name == "reservation-locks":
            return _NamedLock("reservation")
        return _NamedLock("global")

    manifest = smt_cli.build_input_manifest(source)
    resolution = smt_cli.resolve_run_workspace(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=cli_safe_tmp_path / "workspace",
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="skip",
            lock_factory=lock_factory,
        ),
        manifest,
    )
    resolution.workspace_lock = _NamedLock("workspace")

    with pytest.raises(KeyboardInterrupt):
        resolution.close()

    assert released.count("workspace") == 1
    assert released.count("reservation") == 1
    assert resolution.owns_reservation is False


def test_run_releases_reservation_immediately_and_operation_even_if_release_fails(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cli_safe_tmp_path / "Example.zip"
    source.write_bytes(b"fixture")
    workspace = cli_safe_tmp_path / "workspace"
    released: list[str] = []

    class _NamedLock(_ImmediateLock):
        def __init__(self, name: str) -> None:
            self.name = name

        def release(self) -> None:
            released.append(self.name)
            if self.name == "reservation":
                raise OSError(5, "reservation release denied")

    def lock_factory(path: Path, *args: object, **kwargs: object) -> _NamedLock:
        del args, kwargs
        if path.name == "smt-operation.lock":
            return _NamedLock("operation")
        if path.parent.name == "reservation-locks":
            return _NamedLock("reservation")
        return _NamedLock("global")

    class _InitRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            call = {"argv": list(argv), **kwargs}  # type: ignore[arg-type]
            self.calls.append(call)
            script = Path(call["argv"][1]).name  # type: ignore[index]
            if script == "init_workspace.py":
                target = Path(call["argv"][2])  # type: ignore[index]
                (target / ".workflow").mkdir(parents=True)
                (target / "mod").mkdir()
                (target / ".skyrim-chs-workspace.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "kind": smt_cli.WORKSPACE_KIND,
                            "game_id": "skyrim-se",
                        }
                    ),
                    encoding="utf-8",
                )
            return ProcessResult(0, (script,))

    runner = _InitRunner([])
    monkeypatch.setattr(
        smt_cli,
        "advance_workflow",
        lambda *args, **kwargs: smt_cli.CliResult(command="resume", exit_code=0),
    )

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="skip",
            lock_factory=lock_factory,
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.exit_code == 5
    assert released.count("reservation") == 1
    assert released.count("operation") == 1
    assert not any(
        Path(list(call["argv"])[1]).name == "run_translation_queue.py"
        for call in runner.calls
    )


def test_run_operation_lock_timeout_stops_before_tools_or_queue(
    cli_safe_tmp_path: Path,
) -> None:
    source, workspace, _session_value, _store = _create_committed_zip_workspace(
        cli_safe_tmp_path
    )

    class _BusyOperationLock(_ImmediateLock):
        def __init__(self, busy: bool) -> None:
            self.busy = busy

        def acquire(self) -> "_BusyOperationLock":
            if self.busy:
                raise smt_cli.SmtLockTimeoutError("operation held")
            return self

    runner = _RecordingRunner([])

    def lock_factory(path: Path, *args: object, **kwargs: object) -> _BusyOperationLock:
        del args, kwargs
        return _BusyOperationLock(path.name == "smt-operation.lock")

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            tool_setup="auto",
            lock_factory=lock_factory,
        ),
        smt_cli.SmtServices(runner=runner),
    )

    assert result.exit_code == 6
    assert runner.calls == []


def test_run_maps_unsupported_input_and_supervised_timeout(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = cli_safe_tmp_path / "Example.rar"
    unsupported.write_bytes(b"rar")
    unsupported_result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=unsupported,
            game_id="skyrim-se",
            local_state_root=cli_safe_tmp_path / "state",
            workspace_root=cli_safe_tmp_path / "workspaces",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=_RecordingRunner([])),
    )
    assert unsupported_result.exit_code == 4

    source = cli_safe_tmp_path / "Timeout.zip"
    source.write_bytes(b"timeout")
    workspace = cli_safe_tmp_path / "timeout-workspace"

    class _TimeoutInitializerRunner(_RecordingRunner):
        def run(self, argv: object, **kwargs: object) -> ProcessResult:
            self.calls.append({"argv": list(argv), **kwargs})  # type: ignore[arg-type]
            return ProcessResult(124, ("timed out",), timed_out=True)

    result = smt_cli.run_command(
        smt_cli.RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "timeout-state",
            tool_setup="skip",
            lock_factory=lambda *args, **kwargs: _ImmediateLock(),
        ),
        smt_cli.SmtServices(runner=_TimeoutInitializerRunner([])),
    )
    assert result.exit_code == 124
    assert result.underlying_exit_codes == [124]
    assert not workspace.exists()


def _prepare_readonly_workspace(
    root: Path,
    *,
    project_state: str = "qa_failed",
    mod_state: str = "qa_failed",
    generated_at: str = "2026-07-22 14:03:02",
) -> tuple[Path, smt_cli.SmtSession]:
    _source, workspace, session, store = _create_committed_zip_workspace(root)
    store.lock_path.touch()
    (workspace / "qa").mkdir(exist_ok=True)
    (workspace / "qa" / "workflow_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at,
                "game_id": "skyrim-se",
                "game_profile_version": 2,
                "game_display_name": "Skyrim Special Edition",
                "support_level": "stable",
                "interface_translation_encoding": "utf-16-le-bom",
                "policy_path": "config/workflow_policy.json",
                "policy_sha256": "0" * 64,
                "project_state": project_state,
                "states": [
                    {
                        **_state_row(
                            state=mod_state,
                            blockers=["strict_gate_not_clean"]
                            if mod_state in {"blocked", "qa_failed"}
                            else [],
                        ),
                        "last_success_stage": mod_state,
                        "blocking_issues": [],
                        "allowed_scripts": [],
                        "required_files": [],
                        "recommended_actions": [],
                        "repair_candidates": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "qa" / "workflow_tasks.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at,
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    (workspace / ".workflow" / "progress_card.md").write_text(
        "# [SMT 阻断]\n\n原始状态卡\n",
        encoding="utf-8",
    )
    (workspace / ".workflow" / "smt-operation.lock").write_text(
        "pre-existing-lock-metadata", encoding="utf-8"
    )
    (workspace / "config").mkdir(exist_ok=True)
    (workspace / "config" / "tools.local.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    return workspace, session


class _SharedRecordingLock(_ImmediateLock):
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        busy: bool = False,
        release_error: BaseException | None = None,
        on_release: object | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.busy = busy
        self.release_error = release_error
        self.on_release = on_release

    def acquire(self) -> "_SharedRecordingLock":
        self.events.append("acquire")
        if self.busy:
            raise smt_cli.SmtLockTimeoutError("held by writer")
        return self

    def release(self) -> None:
        self.events.append("release")
        if callable(self.on_release):
            self.on_release()
        if self.release_error is not None:
            raise self.release_error


def _readonly_lock_factory(
    events: list[str] | None = None,
    *,
    busy: bool = False,
    release_error: BaseException | None = None,
    on_release: object | None = None,
):
    def factory(path: Path, mode: str, timeout: float, **kwargs: object) -> _SharedRecordingLock:
        assert path.name in {"smt-operation.lock", "cli-state.lock"}
        assert mode == "shared"
        assert timeout >= 0
        assert kwargs.get("command") in {"status", "doctor", "output"}
        return _SharedRecordingLock(
            events=events,
            busy=busy,
            release_error=release_error,
            on_release=on_release,
        )

    return factory


def _tree_snapshot(
    root: Path,
) -> dict[str, tuple[str, int, int, int, int, str | None]]:
    snapshot: dict[str, tuple[str, int, int, int, int, str | None]] = {}
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    for path in paths:
        stat_result = path.lstat()
        is_file = path.is_file() and not path.is_symlink()
        relative = "." if path == root else path.relative_to(root).as_posix()
        snapshot[relative] = (
            "file" if is_file else "directory" if path.is_dir() else "other",
            stat_result.st_mode,
            stat_result.st_mtime_ns,
            stat_result.st_size,
            int(getattr(stat_result, "st_file_attributes", 0)),
            hashlib.sha256(path.read_bytes()).hexdigest() if is_file else None,
        )
    return snapshot


def test_readonly_tree_snapshot_detects_empty_directory_changes(
    cli_safe_tmp_path: Path,
) -> None:
    root = cli_safe_tmp_path / "tree"
    root.mkdir()
    before = _tree_snapshot(root)
    (root / "empty").mkdir()

    assert _tree_snapshot(root) != before


@pytest.mark.parametrize("state", ["blocked", "qa_failed"])
def test_status_reads_blocked_snapshot_without_refresh_or_writes(
    cli_safe_tmp_path: Path,
    state: str,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(
        cli_safe_tmp_path, project_state=state, mod_state=state
    )
    before = _tree_snapshot(workspace)
    events: list[str] = []

    result = smt_cli.status_command(
        smt_cli.StatusRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(events),
        )
    )

    assert result.exit_code == 0
    assert result.outcome == "blocked"
    assert result.workflow_state == state
    assert result.state_snapshot is True
    assert result.refreshed_by_this_command is False
    assert result.state_generated_at == "2026-07-22 14:03:02"
    assert result.state_generated_at_timezone is None
    assert result.progress_card == "# [SMT 阻断]\n\n原始状态卡\n"
    assert events == ["acquire", "release"]
    assert _tree_snapshot(workspace) == before


def test_status_busy_returns_one_without_reading_snapshot(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("busy status must not read workflow files")
        ),
    )

    result = smt_cli.status_command(
        smt_cli.StatusRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(busy=True),
        )
    )

    assert result.exit_code == 1
    assert result.busy is True


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_valid_cwd_never_reads_corrupt_cli_cache(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    store.path.write_text("{not-json", encoding="utf-8")
    store.lock_path.unlink()
    attempted: list[str] = []

    def lock_factory(
        path: Path, mode: str, timeout: float, **kwargs: object
    ) -> _SharedRecordingLock:
        del mode, timeout, kwargs
        attempted.append(path.name)
        if path.name == "cli-state.lock":
            raise AssertionError("valid cwd must not consult the CLI cache")
        return _SharedRecordingLock()

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            cwd=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lock_factory,
        )
    )

    assert result.exit_code == 0
    assert attempted == ["smt-operation.lock"]
    assert not store.lock_path.exists()


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_last_workspace_cache_without_existing_lock_fails_without_creating_it(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    last_root = cli_safe_tmp_path / "last"
    last_root.mkdir()
    last_workspace, _session_value = _prepare_readonly_workspace(last_root)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(last_workspace)
    store.write(cache)

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            cwd=cli_safe_tmp_path,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 1
    assert not store.lock_path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="LockFileEx is Windows-only")
def test_cli_cache_shared_reader_and_atomic_writer_do_not_race(
    cli_safe_tmp_path: Path,
) -> None:
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    store.write(smt_cli._empty_cli_state())
    store.lock_path.touch()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait()
            for index in range(75):
                lock = smt_cli.SmtProcessFileLock(
                    store.lock_path,
                    "exclusive",
                    5,
                    command="cache-writer-probe",
                )
                lock.acquire()
                try:
                    state = store.read()
                    state["last_workspace"] = str(cli_safe_tmp_path / str(index))
                    store.write(state)
                finally:
                    lock.release()
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _index in range(75):
                smt_cli._read_cli_state_shared_no_create(
                    store,
                    smt_cli.SmtProcessFileLock,
                    5,
                    command="status",
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_readonly_projection_rejects_non_string_state_generated_at(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    state_path = workspace / "qa" / "workflow_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generated_at"] = 123
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 1
    assert result.outcome is None
    assert "generated_at" in "\n".join(result.diagnostics)


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_readonly_projection_rejects_malformed_task_contract(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    tasks_path = workspace / "qa" / "workflow_tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks["tasks"] = [{"task_id": "partial"}]
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 1
    assert result.outcome is None
    assert "workflow tasks" in "\n".join(result.diagnostics)
    assert result.outcome is None


def test_status_missing_progress_card_is_read_failure_not_identity_conflict(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    (workspace / ".workflow" / "progress_card.md").unlink()

    result = smt_cli.status_command(
        smt_cli.StatusRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 1
    assert result.outcome is None
    assert not (workspace / ".workflow" / "progress_card.md").exists()


def test_status_invalid_session_returns_identity_conflict(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    (workspace / smt_cli.SESSION_RELATIVE_PATH).write_text("{}", encoding="utf-8")

    result = smt_cli.status_command(
        smt_cli.StatusRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 6


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_readonly_commands_skip_invalid_cwd_marker_and_use_valid_last_workspace(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    last_root = cli_safe_tmp_path / "last"
    last_root.mkdir()
    last_workspace, _session_value = _prepare_readonly_workspace(last_root)
    invalid_cwd = cli_safe_tmp_path / "invalid-cwd"
    invalid_cwd.mkdir()
    (invalid_cwd / smt_cli.WORKSPACE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 999,
                "kind": smt_cli.WORKSPACE_KIND,
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(last_workspace)
    store.write(cache)
    store.lock_path.touch()

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            cwd=invalid_cwd,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 0
    assert result.workspace == str(last_workspace)


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_explicit_invalid_workspace_never_falls_back_to_last(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    last_root = cli_safe_tmp_path / "last"
    last_root.mkdir()
    last_workspace, _session_value = _prepare_readonly_workspace(last_root)
    invalid = cli_safe_tmp_path / "explicit-invalid"
    invalid.mkdir()
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(last_workspace)
    store.write(cache)

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            workspace=invalid,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 6
    assert result.workspace == str(invalid)


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_busy_cwd_candidate_never_falls_back_to_last(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    cwd_root = cli_safe_tmp_path / "cwd-root"
    cwd_root.mkdir()
    cwd_workspace, _cwd_session = _prepare_readonly_workspace(cwd_root)
    last_root = cli_safe_tmp_path / "last-root"
    last_root.mkdir()
    last_workspace, _last_session = _prepare_readonly_workspace(last_root)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(last_workspace)
    store.write(cache)
    attempted: list[Path] = []

    def lock_factory(
        path: Path, mode: str, timeout: float, **kwargs: object
    ) -> _SharedRecordingLock:
        del mode, timeout, kwargs
        attempted.append(path)
        if path.parent.parent == cwd_workspace:
            return _SharedRecordingLock(busy=True)
        raise AssertionError("busy cwd candidate must stop before last workspace")

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            cwd=cwd_workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lock_factory,
        )
    )

    assert result.exit_code == 1
    assert result.busy is True
    assert attempted == [cwd_workspace / smt_cli.WORKSPACE_LOCK_RELATIVE_PATH]


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
        (smt_cli.DoctorRequest, smt_cli.doctor_command),
    ],
)
def test_readonly_candidate_validation_interrupt_releases_before_returning_130(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        smt_cli,
        "validate_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    result = runner(  # type: ignore[operator]
        request_type(  # type: ignore[operator]
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(events),
        )
    )

    assert result.exit_code == 130
    assert events == (
        ["acquire", "release", "acquire", "release"]
        if request_type is smt_cli.DoctorRequest
        else ["acquire", "release"]
    )


def test_readonly_candidate_validation_system_exit_releases_then_propagates(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        smt_cli,
        "validate_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SystemExit("validation exit")
        ),
    )

    with pytest.raises(SystemExit, match="validation exit"):
        smt_cli.status_command(
            smt_cli.StatusRequest(
                workspace=workspace,
                local_state_root=cli_safe_tmp_path / "state",
                lock_factory=_readonly_lock_factory(events),
            )
        )

    assert events == ["acquire", "release"]


@pytest.mark.parametrize(
    ("release_error", "expected_exit", "raises_system_exit"),
    [
        (OSError("release denied"), 5, False),
        (SystemExit("release exit"), None, True),
    ],
)
def test_candidate_validation_release_failure_follows_control_flow_contract(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_error: BaseException,
    expected_exit: int | None,
    raises_system_exit: bool,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        smt_cli,
        "validate_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            smt_cli.WorkspaceConflictError("invalid during validation")
        ),
    )
    request = smt_cli.StatusRequest(
        workspace=workspace,
        local_state_root=cli_safe_tmp_path / "state",
        lock_factory=_readonly_lock_factory(
            events,
            release_error=release_error,
        ),
    )

    if raises_system_exit:
        with pytest.raises(SystemExit, match="release exit"):
            smt_cli.status_command(request)
    else:
        result = smt_cli.status_command(request)
        assert result.exit_code == expected_exit

    assert events == ["acquire", "release"]


def test_doctor_is_read_only_and_scans_only_direct_default_workspaces(
    cli_safe_tmp_path: Path,
) -> None:
    workspace_root = cli_safe_tmp_path / "workspaces"
    direct_workspace, _session_value = _prepare_readonly_workspace(
        cli_safe_tmp_path
    )
    nested_parent = workspace_root / "NestedParent"
    nested_parent.mkdir()
    nested_workspace, _nested_session = _prepare_readonly_workspace(
        nested_parent
    )
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(workspace_root / "Missing")
    cache["input_mappings"] = {"broken": str(workspace_root / "Missing")}
    cache["reservations"] = {
        "00000000-0000-4000-8000-000000000123": {
            "workspace_id": "00000000-0000-4000-8000-000000000123",
            "path": str(workspace_root / "Reserved"),
            "fingerprint_identity": "reserved-identity",
            "pid": 123,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(cache)
    store.lock_path.touch()
    before_workspace = _tree_snapshot(workspace_root)
    before_cache = _tree_snapshot(cli_safe_tmp_path / "state")

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            cwd=cli_safe_tmp_path,
            workspace_root=workspace_root,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    combined = "\n".join([*result.details, *result.diagnostics])
    assert result.exit_code == 0
    assert result.state_snapshot is False
    assert str(direct_workspace) in combined
    assert str(nested_workspace) not in combined
    assert "reservation" in combined.casefold()
    assert "mapping" in combined.casefold()
    assert _tree_snapshot(workspace_root) == before_workspace
    assert _tree_snapshot(cli_safe_tmp_path / "state") == before_cache


def test_doctor_invalid_existing_last_does_not_prevent_direct_default_scan(
    cli_safe_tmp_path: Path,
) -> None:
    fixture_root = cli_safe_tmp_path / "fixture"
    fixture_root.mkdir()
    valid_workspace, _session_value = _prepare_readonly_workspace(fixture_root)
    workspace_root = fixture_root / "workspaces"
    nonworkspace = cli_safe_tmp_path / "existing-nonworkspace"
    nonworkspace.mkdir()
    (nonworkspace / "keep.txt").write_text("keep", encoding="utf-8")
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = str(nonworkspace)
    store.write(cache)
    store.lock_path.touch()
    before_root = _tree_snapshot(workspace_root)
    before_nonworkspace = _tree_snapshot(nonworkspace)
    before_cache = _tree_snapshot(cli_safe_tmp_path / "state")

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            cwd=cli_safe_tmp_path,
            workspace_root=workspace_root,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    combined = "\n".join([*result.details, *result.diagnostics])
    assert result.exit_code == 0
    assert str(valid_workspace) in combined
    assert _tree_snapshot(workspace_root) == before_root
    assert _tree_snapshot(nonworkspace) == before_nonworkspace
    assert _tree_snapshot(cli_safe_tmp_path / "state") == before_cache


def test_doctor_reads_known_folders_and_plugin_version_through_services(
    cli_safe_tmp_path: Path,
) -> None:
    documents = cli_safe_tmp_path / "KnownDocuments"
    local_app_data = cli_safe_tmp_path / "KnownLocalAppData"
    documents.mkdir()
    local_app_data.mkdir()

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            cwd=cli_safe_tmp_path,
            workspace_root=documents / "Workspaces",
            local_state_root=local_app_data / "State",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(
            documents_provider=lambda: documents,
            local_app_data_provider=lambda: local_app_data,
        ),
    )

    details = "\n".join(result.details)
    assert result.exit_code == 0
    assert str(documents) in details
    assert str(local_app_data) in details
    assert "Plugin version: 0.4.0" in details


def test_doctor_reports_extra_input_without_cleaning_it(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    extra = workspace / "mod" / "Unregistered.zip"
    extra.write_bytes(b"other")
    before = _tree_snapshot(workspace)

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            workspace_root=cli_safe_tmp_path / "workspaces",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 0
    assert result.workspace == str(workspace)
    assert result.mod_name == "ExampleMod"
    assert "mod/Unregistered.zip" in "\n".join(result.diagnostics)
    assert extra.exists()
    assert _tree_snapshot(workspace) == before


def test_doctor_does_not_create_lock_or_workflow_directory_for_unregistered_child(
    cli_safe_tmp_path: Path,
) -> None:
    workspace_root = cli_safe_tmp_path / "workspaces"
    unregistered = workspace_root / "PlainDirectory"
    unregistered.mkdir(parents=True)

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            cwd=cli_safe_tmp_path,
            workspace_root=workspace_root,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=smt_cli.SmtProcessFileLock,
        )
    )

    assert result.exit_code == 0
    assert not (unregistered / ".workflow").exists()
    assert "missing or unsafe" in "\n".join(result.diagnostics)


def test_doctor_validates_existing_cache_mapping_identity_without_rebinding(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, session = _prepare_readonly_workspace(cli_safe_tmp_path)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    cache = store.read()
    cache["last_workspace"] = None
    cache["input_mappings"] = {
        f"{session.input_identity}-wrong": str(workspace),
    }
    store.write(cache)
    store.lock_path.touch()
    before = _tree_snapshot(cli_safe_tmp_path / "state")

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            cwd=cli_safe_tmp_path,
            workspace_root=cli_safe_tmp_path / "empty-workspace-root",
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    combined = "\n".join(result.diagnostics).casefold()
    assert result.exit_code == 0
    assert "mapping" in combined
    assert "identity" in combined
    assert _tree_snapshot(cli_safe_tmp_path / "state") == before


@pytest.mark.parametrize("addressing", ["explicit", "cwd"])
def test_doctor_always_runs_one_independent_global_cache_diagnostic(
    cli_safe_tmp_path: Path,
    addressing: str,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    stale = cli_safe_tmp_path / "missing-mapped-workspace"
    cache = store.read()
    cache["input_mappings"]["stale-identity"] = str(stale)
    cache["reservations"] = {
        "00000000-0000-4000-8000-000000000777": {
            "workspace_id": "00000000-0000-4000-8000-000000000777",
            "path": str(cli_safe_tmp_path / "reserved-workspace"),
            "fingerprint_identity": "reserved-fingerprint",
            "pid": 777,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(cache)
    lock_paths: list[str] = []

    def lock_factory(
        path: Path, mode: str, timeout: float, **kwargs: object
    ) -> _SharedRecordingLock:
        del mode, timeout, kwargs
        lock_paths.append(path.name)
        return _SharedRecordingLock()

    request_kwargs = {
        "workspace" if addressing == "explicit" else "cwd": workspace,
        "local_state_root": cli_safe_tmp_path / "state",
        "lock_factory": lock_factory,
    }
    result = smt_cli.doctor_command(smt_cli.DoctorRequest(**request_kwargs))

    combined = "\n".join(result.diagnostics).casefold()
    assert result.exit_code == 0
    assert result.workspace == str(workspace)
    assert "stale input mapping" in combined
    assert "reservation pending" in combined
    assert lock_paths.count("cli-state.lock") == 1
    assert lock_paths.count("smt-operation.lock") == 1


@pytest.mark.parametrize(
    ("cache_condition", "expected_diagnostic"),
    [
        ("busy", "cache busy"),
        ("corrupt", "cache unreadable"),
        ("missing", "cache missing"),
    ],
)
def test_doctor_cache_diagnostic_failure_does_not_block_explicit_workspace(
    cli_safe_tmp_path: Path,
    cache_condition: str,
    expected_diagnostic: str,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    store = smt_cli.CliStateStore(cli_safe_tmp_path / "state")
    if cache_condition == "corrupt":
        store.path.write_text("{broken", encoding="utf-8")
    elif cache_condition == "missing":
        store.path.unlink()
        store.lock_path.unlink()

    def lock_factory(
        path: Path, mode: str, timeout: float, **kwargs: object
    ) -> _SharedRecordingLock:
        del mode, timeout, kwargs
        return _SharedRecordingLock(
            busy=cache_condition == "busy" and path.name == "cli-state.lock"
        )

    result = smt_cli.doctor_command(
        smt_cli.DoctorRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=lock_factory,
        )
    )

    assert result.exit_code == 0
    assert result.workspace == str(workspace)
    assert expected_diagnostic in "\n".join(result.diagnostics).casefold()


def test_output_missing_artifacts_succeeds_and_reports_two_manual_states(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(
        cli_safe_tmp_path,
        project_state="ready_for_manual_test",
        mod_state="ready_for_manual_test",
    )

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert result.exit_code == 0
    assert result.output_paths["root"]["path"] == str(workspace)
    assert result.output_paths["package_directory"]["path"] == str(
        workspace / "out" / "ExampleMod" / "汉化产出"
    )
    assert (
        result.output_paths["package_directory"]["path"]
        != result.output_paths["root"]["path"]
    )
    assert result.output_paths["final_mod"]["exists"] is False
    assert result.output_paths["intermediate"]["exists"] is False
    assert result.output_paths["package"]["exists"] is False
    assert "可以进入人工游戏测试：是" in result.details
    assert "人工游戏测试已验证：否" in result.details
    assert "允许交付" not in "\n".join(result.details)


def test_output_reports_manual_test_validation_separately(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(
        cli_safe_tmp_path,
        project_state="manual_tested",
        mod_state="manual_tested",
    )

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(),
        )
    )

    assert "可以进入人工游戏测试：是" in result.details
    assert "人工游戏测试已验证：是" in result.details


def test_output_busy_returns_one_without_reading_snapshot(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    monkeypatch.setattr(
        smt_cli,
        "read_workflow_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("busy output must not read workflow files")
        ),
    )

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            lock_factory=_readonly_lock_factory(busy=True),
        )
    )

    assert result.exit_code == 1
    assert result.busy is True


@pytest.mark.parametrize(
    ("target", "relative"),
    [
        ("root", None),
        ("final-mod", "out/ExampleMod/汉化产出/final_mod"),
        ("intermediate", "out/ExampleMod/汉化产出/intermediate"),
        ("package-directory", "out/ExampleMod/汉化产出"),
    ],
)
def test_output_open_allowlist_releases_shared_lock_before_opening(
    cli_safe_tmp_path: Path,
    target: str,
    relative: str | None,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    selected = workspace if relative is None else workspace / relative
    selected.mkdir(parents=True, exist_ok=True)
    events: list[str] = []
    opened: list[Path] = []

    def opener(path: Path) -> None:
        assert events == ["acquire", "release"]
        opened.append(path)

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target=target,
            lock_factory=_readonly_lock_factory(events),
        ),
        smt_cli.ReadOnlyServices(opener=opener),
    )

    assert result.exit_code == 0
    assert opened == [selected]


@pytest.mark.parametrize("target", ["anything", "final-mod"])
def test_output_does_not_open_invalid_or_missing_target(
    cli_safe_tmp_path: Path,
    target: str,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    opened: list[Path] = []

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target=target,
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(opener=opened.append),
    )

    assert result.exit_code == 1
    assert opened == []


def test_output_revalidates_target_after_lock_release_before_open(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    replacement = workspace / "replacement"
    replacement.mkdir()
    opened: list[Path] = []

    def replace_after_release() -> None:
        final_mod.rmdir()
        replacement.rename(final_mod)

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(on_release=replace_after_release),
        ),
        smt_cli.ReadOnlyServices(opener=opened.append),
    )

    assert result.exit_code == 1
    assert opened == []


@pytest.mark.skipif(sys.platform != "win32", reason="directory pinning is Windows-only")
def test_output_pins_directory_until_opener_returns(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    moved = workspace / "moved-while-open"
    moved_parent = workspace / "moved-parent-while-open"
    rename_errors: list[OSError] = []

    def opener(path: Path) -> None:
        for source, destination in ((path, moved), (path.parent, moved_parent)):
            try:
                source.rename(destination)
            except OSError as exc:
                rename_errors.append(exc)

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(opener=opener),
    )

    if len(rename_errors) == 2:
        assert result.exit_code == 0
        assert final_mod.is_dir()
        final_mod.rename(workspace / "moved-after-open")
    else:
        assert result.exit_code == 1
        assert moved.is_dir() or moved_parent.is_dir()


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_output_pinned_handle_context_closes_for_control_flow_signals(
    cli_safe_tmp_path: Path,
    signal_type: type[BaseException],
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    events: list[str] = []

    class _PinnedContext:
        def __enter__(self) -> "_PinnedContext":
            events.append("pin")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("unpin")

    def pinner(_path: Path, _workspace: Path) -> _PinnedContext:
        return _PinnedContext()

    def opener(_path: Path) -> None:
        raise signal_type("control flow")

    request = smt_cli.OutputRequest(
        workspace=workspace,
        local_state_root=cli_safe_tmp_path / "state",
        open_target="final-mod",
        lock_factory=_readonly_lock_factory(),
    )
    services = smt_cli.ReadOnlyServices(opener=opener, directory_pinner=pinner)
    if signal_type is SystemExit:
        with pytest.raises(SystemExit, match="control flow"):
            smt_cli.output_command(request, services)
    else:
        result = smt_cli.output_command(request, services)
        assert result.exit_code == 130

    assert events == ["pin", "unpin"]


@pytest.mark.parametrize(
    ("signal_type", "expected_exit"),
    [
        (KeyboardInterrupt, 130),
        (SystemExit, None),
        (GeneratorExit, None),
    ],
)
def test_output_preserves_control_flow_when_pinned_cleanup_also_fails(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal_type: type[BaseException],
    expected_exit: int | None,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    pinned = smt_cli.PinnedDirectoryHandle(final_mod, workspace)
    monkeypatch.setattr(
        pinned,
        "acquire",
        lambda: pinned,
    )

    def failing_release() -> None:
        pinned.cleanup_errors.append("CloseHandle failed in deterministic probe")
        raise smt_cli.ManagedProcessEnvironmentError("cleanup probe failed")

    monkeypatch.setattr(pinned, "release", failing_release)

    def opener(_path: Path) -> None:
        raise signal_type("primary control flow")

    request = smt_cli.OutputRequest(
        workspace=workspace,
        local_state_root=cli_safe_tmp_path / "state",
        open_target="final-mod",
        lock_factory=_readonly_lock_factory(),
    )
    services = smt_cli.ReadOnlyServices(
        opener=opener,
        directory_pinner=lambda *_args: pinned,
    )
    if signal_type is KeyboardInterrupt:
        result = smt_cli.output_command(request, services)
        assert result.exit_code == expected_exit
        assert "CloseHandle failed" in "\n".join(result.diagnostics)
    else:
        with pytest.raises(signal_type, match="primary control flow") as caught:
            smt_cli.output_command(request, services)
        notes = getattr(caught.value, "__notes__", [])
        assert any("cleanup probe failed" in note for note in notes)


def test_output_maps_pinned_cleanup_failure_without_body_exception_to_five(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    pinned = smt_cli.PinnedDirectoryHandle(final_mod, workspace)
    monkeypatch.setattr(pinned, "acquire", lambda: pinned)
    monkeypatch.setattr(
        pinned,
        "release",
        lambda: (_ for _ in ()).throw(
            smt_cli.ManagedProcessEnvironmentError("cleanup probe failed")
        ),
    )

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(
            opener=lambda _path: None,
            directory_pinner=lambda *_args: pinned,
        ),
    )

    assert result.exit_code == 5


@pytest.mark.parametrize("body_error_type", [OSError, ValueError])
def test_output_preserves_regular_body_failure_and_reports_unique_cleanup_markers(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_error_type: type[Exception],
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.mkdir(parents=True)
    pinned = smt_cli.PinnedDirectoryHandle(final_mod, workspace)
    monkeypatch.setattr(pinned, "acquire", lambda: pinned)
    cleanup_markers = [f"cleanup-marker-{index}" for index in range(205)]

    def failing_release() -> None:
        pinned.cleanup_errors.extend([*cleanup_markers, cleanup_markers[-1]])
        raise smt_cli.ManagedProcessEnvironmentError("secondary cleanup failure")

    monkeypatch.setattr(pinned, "release", failing_release)

    def opener(_path: Path) -> None:
        raise body_error_type("primary opener marker")

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(
            opener=opener,
            directory_pinner=lambda *_args: pinned,
        ),
    )

    assert result.exit_code == 1
    assert result.message == "requested output directory changed or could not be opened"
    assert "primary opener marker" in "\n".join(result.diagnostics)
    assert cleanup_markers[-1] in result.diagnostics
    assert len(result.diagnostics) == 200
    assert len(result.diagnostics) == len(set(result.diagnostics))


def test_pinned_directory_release_best_effort_closes_every_owned_handle(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class _Kernel32:
        @staticmethod
        def CloseHandle(handle: int) -> bool:
            closed.append(handle)
            return handle != 12

    monkeypatch.setattr(
        smt_windows,
        "_win32_bindings",
        lambda: type("Bindings", (), {"kernel32": _Kernel32()})(),
    )
    pinned = smt_windows.PinnedDirectoryHandle(
        cli_safe_tmp_path,
        cli_safe_tmp_path,
    )
    pinned._handles = [11, 12, 13]
    pinned._handle = 13

    with pytest.raises(
        smt_windows.ManagedProcessEnvironmentError,
        match="cleanup failed",
    ):
        pinned.release()

    assert closed == [13, 12, 11]
    assert pinned._handles == []
    assert pinned._handle is None
    assert pinned.cleanup_errors


def test_output_rejects_reparse_target_without_opening(
    cli_safe_tmp_path: Path,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    final_mod = workspace / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    final_mod.parent.mkdir(parents=True)
    outside = cli_safe_tmp_path / "outside"
    outside.mkdir()
    try:
        final_mod.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    opened: list[Path] = []

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(opener=opened.append),
    )

    assert result.exit_code == 1
    assert opened == []


def test_output_rejects_helper_path_that_escapes_workspace(
    cli_safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    outside = cli_safe_tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(smt_cli, "final_mod_dir", lambda *_args: outside)
    opened: list[Path] = []

    result = smt_cli.output_command(
        smt_cli.OutputRequest(
            workspace=workspace,
            local_state_root=cli_safe_tmp_path / "state",
            open_target="final-mod",
            lock_factory=_readonly_lock_factory(),
        ),
        smt_cli.ReadOnlyServices(opener=opened.append),
    )

    assert result.exit_code == 1
    assert opened == []


@pytest.mark.parametrize(
    ("command", "request_type", "runner"),
    [
        ("status", smt_cli.StatusRequest, smt_cli.status_command),
        ("output", smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_readonly_lock_release_failure_uses_environment_result(
    cli_safe_tmp_path: Path,
    command: str,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    request = request_type(  # type: ignore[operator]
        workspace=workspace,
        local_state_root=cli_safe_tmp_path / "state",
        lock_factory=_readonly_lock_factory(release_error=OSError("release denied")),
    )

    result = runner(request)  # type: ignore[operator]

    assert result.command == command
    assert result.exit_code == 5
    assert "release" in "\n".join(result.diagnostics).casefold()


@pytest.mark.parametrize(
    ("request_type", "runner"),
    [
        (smt_cli.StatusRequest, smt_cli.status_command),
        (smt_cli.OutputRequest, smt_cli.output_command),
    ],
)
def test_readonly_lock_release_keyboard_interrupt_returns_130(
    cli_safe_tmp_path: Path,
    request_type: object,
    runner: object,
) -> None:
    workspace, _session_value = _prepare_readonly_workspace(cli_safe_tmp_path)
    request = request_type(  # type: ignore[operator]
        workspace=workspace,
        local_state_root=cli_safe_tmp_path / "state",
        lock_factory=_readonly_lock_factory(release_error=KeyboardInterrupt()),
    )

    result = runner(request)  # type: ignore[operator]

    assert result.exit_code == 130
    assert "KeyboardInterrupt" in "\n".join(result.diagnostics)


def _load_smt_entry_module() -> object:
    return importlib.import_module("smt")


def test_smt_public_help_lists_exact_command_surface() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIRECTORY / "smt.py"), "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    for command in ("run", "status", "resume", "doctor", "output"):
        assert command in completed.stdout

    smt = _load_smt_entry_module()
    command_action = next(
        action for action in smt.build_parser()._actions if action.dest == "command"
    )
    assert set(command_action.choices) == {
        "run",
        "status",
        "resume",
        "doctor",
        "output",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["unknown"],
        ["run", "fixture.zip"],
        ["run", "--game", "skyrim-se"],
        ["output", "--open", "arbitrary-path"],
    ],
)
def test_smt_argparse_errors_remain_exit_2(argv: list[str]) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIRECTORY / "smt.py"), *argv],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "error:" in completed.stderr


def test_smt_json_renderer_emits_one_complete_object(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smt = _load_smt_entry_module()
    result = smt_cli.empty_result("status")
    result.exit_code = 0

    monkeypatch.setattr(smt_cli, "dispatch", lambda _request: result)

    exit_code = smt.main(["--format", "json", "status"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["command"] == "status"
    assert payload["state_generated_at_timezone"] is None


def test_smt_text_renderer_preserves_progress_card_and_shows_actions(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smt = _load_smt_entry_module()
    progress_card = "[SMT 进度]\n\n| 项目 | 状态 |\n| --- | --- |\n| 当前 | 等待 |\n"
    result = smt_cli.empty_result("run")
    result.exit_code = 3
    result.outcome = "needs_agent_translation"
    result.message = "需要 Agent 生成译文"
    result.progress_card = progress_card
    result.next_action = {
        "kind": "agent_translation",
        "summary": "翻译候选文本",
        "artifacts": ["work/normalized/ExampleMod/candidates.json"],
    }
    result.output_paths["final_mod"] = {
        "path": "out/ExampleMod/汉化产出/final_mod",
        "exists": False,
        "kind": "directory",
        "validated": None,
        "validation_evidence": None,
    }
    result.details.append("可以进入人工游戏测试：否")
    result.diagnostics.append("等待译文")

    monkeypatch.setattr(smt_cli, "dispatch", lambda _request: result)

    exit_code = smt.main(["run", "fixture.zip", "--game", "skyrim-se"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.err == ""
    assert progress_card in captured.out
    assert "needs_agent_translation" in captured.out
    assert "翻译候选文本" in captured.out
    assert "work/normalized/ExampleMod/candidates.json" in captured.out
    assert "out/ExampleMod/汉化产出/final_mod" in captured.out
    assert "exists: no" in captured.out
    assert "可以进入人工游戏测试：否" in captured.out
    assert "等待译文" in captured.out
    assert "completed" not in captured.out


def test_smt_parser_builds_the_frozen_run_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smt = _load_smt_entry_module()
    seen: list[object] = []
    result = smt_cli.empty_result("run")
    result.exit_code = 0

    monkeypatch.setattr(
        smt_cli,
        "dispatch",
        lambda namespace: seen.append(namespace) or result,
    )

    exit_code = smt.main(
        [
            "--format",
            "json",
            "run",
            "fixture.zip",
            "--game",
            "fallout4",
            "--workspace",
            "D:/Work/Explicit",
            "--workspace-root",
            "D:/Work",
            "--tool-setup",
            "manual",
            "--timeout-seconds",
            "45.5",
        ],
    )

    assert exit_code == 0
    assert len(seen) == 1
    namespace = seen[0]
    assert namespace.command == "run"
    assert namespace.input == "fixture.zip"
    assert namespace.game == "fallout4"
    assert namespace.workspace == "D:/Work/Explicit"
    assert namespace.workspace_root == "D:/Work"
    assert namespace.tool_setup == "manual"
    assert namespace.timeout_seconds == 45.5


@pytest.mark.parametrize("command", ["run", "status", "resume", "doctor", "output"])
def test_dispatch_non_windows_fails_before_reading_request_fields(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    class NonWindowsNamespace:
        def __init__(self, selected_command: str) -> None:
            self.command = selected_command

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"dispatch accessed {name} before platform gating")

    monkeypatch.setattr(smt_cli, "_is_windows_platform", lambda: False)

    result = smt_cli.dispatch(NonWindowsNamespace(command))

    assert result.command == command
    assert result.exit_code == 5
    assert result.outcome is None
    assert set(result.to_payload()) == EXPECTED_PAYLOAD_KEYS
    assert "Windows" in result.message


@pytest.mark.parametrize(
    ("routed_request", "runner_name", "service_type"),
    [
        (smt_cli.RunRequest(Path("fixture.zip"), "skyrim-se"), "run_command", smt_cli.SmtServices),
        (smt_cli.ResumeRequest(), "resume_command", smt_cli.SmtServices),
        (smt_cli.StatusRequest(), "status_command", smt_cli.ReadOnlyServices),
        (smt_cli.DoctorRequest(), "doctor_command", smt_cli.ReadOnlyServices),
        (smt_cli.OutputRequest(), "output_command", smt_cli.ReadOnlyServices),
    ],
)
def test_dispatch_routes_typed_requests_with_real_service_defaults(
    monkeypatch: pytest.MonkeyPatch,
    routed_request: object,
    runner_name: str,
    service_type: type[object],
) -> None:
    calls: list[tuple[object, object]] = []
    expected = smt_cli.empty_result(runner_name.removesuffix("_command"))
    monkeypatch.setattr(smt_cli, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        smt_cli,
        runner_name,
        lambda routed_request, services: calls.append((routed_request, services)) or expected,
    )

    result = smt_cli.dispatch(routed_request)

    assert result is expected
    assert calls[0][0] is routed_request
    assert isinstance(calls[0][1], service_type)


@pytest.mark.parametrize(
    ("argv", "request_type", "expected_fields"),
    [
        (
            [
                "run",
                "fixture.zip",
                "--game",
                "skyrim-se",
                "--workspace",
                "D:/Workspace",
                "--workspace-root",
                "D:/Root",
                "--tool-setup",
                "skip",
                "--timeout-seconds",
                "61",
            ],
            smt_cli.RunRequest,
            {
                "source": Path("fixture.zip"),
                "game_id": "skyrim-se",
                "workspace": Path("D:/Workspace"),
                "workspace_root": Path("D:/Root"),
                "tool_setup": "skip",
                "timeout_seconds": 61.0,
            },
        ),
        (
            ["resume", "--workspace", "D:/Workspace", "--timeout-seconds", "62"],
            smt_cli.ResumeRequest,
            {"workspace": Path("D:/Workspace"), "timeout_seconds": 62.0},
        ),
        (
            ["status", "--workspace", "D:/Workspace"],
            smt_cli.StatusRequest,
            {"workspace": Path("D:/Workspace")},
        ),
        (
            ["doctor", "--workspace", "D:/Workspace"],
            smt_cli.DoctorRequest,
            {"workspace": Path("D:/Workspace")},
        ),
        (
            ["output", "--workspace", "D:/Workspace", "--open", "intermediate"],
            smt_cli.OutputRequest,
            {"workspace": Path("D:/Workspace"), "open_target": "intermediate"},
        ),
    ],
)
def test_dispatch_converts_public_namespaces_to_typed_requests(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    request_type: type[object],
    expected_fields: dict[str, object],
) -> None:
    smt = _load_smt_entry_module()
    namespace = smt.build_parser().parse_args(argv)
    captured: list[object] = []
    command = namespace.command
    runner_name = f"{command}_command"
    expected = smt_cli.empty_result(command)
    monkeypatch.setattr(smt_cli, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        smt_cli,
        runner_name,
        lambda request, _services: captured.append(request) or expected,
    )

    result = smt_cli.dispatch(namespace)

    assert result is expected
    assert len(captured) == 1
    assert isinstance(captured[0], request_type)
    for field_name, expected_value in expected_fields.items():
        assert getattr(captured[0], field_name) == expected_value


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows CI contract")
@pytest.mark.parametrize(
    "argv",
    [
        ["run", "missing.zip", "--game", "skyrim-se"],
        ["status"],
        ["resume"],
        ["doctor"],
        ["output"],
    ],
)
def test_smt_real_non_windows_commands_return_schema_exit_5(argv: list[str]) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIRECTORY / "smt.py"),
            "--format",
            "json",
            *argv,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 5
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout
    assert len(completed.stdout.splitlines()) == 1
    payload = json.loads(completed.stdout)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["command"] == argv[0]
    assert payload["exit_code"] == 5
    assert payload["outcome"] is None


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_smt_business_output_is_utf8_when_python_stdio_is_cp936(
    output_format: str,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp936"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIRECTORY / "smt.py"),
            "--format",
            output_format,
            "run",
            "missing-🙂.zip",
            "--game",
            "skyrim-se",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 4
    assert completed.stderr == b""
    rendered = completed.stdout.decode("utf-8")
    assert "Traceback" not in rendered
    assert "🙂" in rendered
    if output_format == "json":
        assert len(rendered.splitlines()) == 1
        payload = json.loads(rendered)
        assert payload["exit_code"] == 4
        assert payload["command"] == "run"
    else:
        assert "Outcome: -" in rendered
        assert "input format or safety policy is unsupported" in rendered


def _repo_text(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _python_script_command_refs(text: str) -> set[str]:
    return {
        match.replace("\\", "/").lower()
        for match in re.findall(
            r"(?im)^\s*(?:python|uv\s+run)\s+(?:\.\\)?(scripts[\\/][a-z0-9_.-]+\.py)\b",
            text,
        )
    }


@pytest.mark.parametrize("relative_path", ["README.md", "USER_GUIDE.md"])
def test_public_docs_expose_only_the_five_smt_commands(relative_path: str) -> None:
    text = _repo_text(relative_path)

    assert _python_script_command_refs(text) == {"scripts/smt.py"}
    for command in ("run", "status", "resume", "doctor", "output"):
        assert re.search(rf"python scripts\\smt\.py(?: --format json)? {command}\b", text)
    assert "Documents/SkyrimModTranslationWorkspaces" in text
    assert all(token in text for token in ("ZIP", "7Z", "目录", "--workspace", "--tool-setup"))
    assert all(
        token in text
        for token in (
            "completed",
            "ready_for_manual_test",
            "needs_agent_translation",
            "needs_gui",
            "needs_user_input",
            "blocked",
            "状态快照",
            "只读诊断",
        )
    )
    assert "人工游戏测试已验证" in text
    assert "可以进入人工游戏测试" in text


def test_public_docs_document_exit_codes_and_new_workspace_default() -> None:
    combined = _repo_text("README.md") + _repo_text("USER_GUIDE.md")

    for exit_code in ("`0`", "`1`", "`2`", "`3`", "`4`", "`5`", "`6`", "`124`", "`130`"):
        assert exit_code in combined
    assert "每个新输入" in combined
    assert "新工作区" in combined
    assert "同一输入" in combined


def test_workflow_policy_never_authorizes_or_names_smt_outer_controller() -> None:
    policy = json.loads(_repo_text("config/workflow_policy.json"))
    serialized = json.dumps(policy, ensure_ascii=False).replace("\\", "/").lower()

    assert "scripts/smt.py" not in serialized
    assert "public_control_entrypoints" not in serialized
    for relative_path in ("scripts/write_workflow_state.py", "scripts/write_workflow_tasks.py"):
        assert "scripts/smt.py" not in _repo_text(relative_path).replace("\\", "/").lower()


def test_agent_public_entry_contract_uses_smt_json_and_forbids_manual_composition() -> None:
    entry_skill = _repo_text("skills/skyrim-mod-chs-translation/SKILL.md")
    agents = _repo_text("AGENTS.md")
    public_section = agents.split("## 唯一公开 CLI 入口", 1)[1].split("\n## ", 1)[0]

    for text in (entry_skill, public_section):
        assert "python scripts\\smt.py --format json run" in text
        for command in ("resume", "status", "doctor", "output"):
            assert f"python scripts\\smt.py --format json {command}" in text
        assert "不得自行组合" in text
        assert _python_script_command_refs(text) == {"scripts/smt.py"}


@pytest.mark.parametrize(
    "relative_path",
    [
        "skills/skyrim-mod-translation-orchestrator/SKILL.md",
        "skills/workflow-agent-orchestration/SKILL.md",
        "skills/workflow-policy-and-state/SKILL.md",
    ],
)
def test_agent_internal_skills_keep_the_outer_controller_out_of_workflow_tasks(
    relative_path: str,
) -> None:
    text = _repo_text(relative_path)

    assert "python scripts\\smt.py" in text
    assert "内部实现" in text
    assert "workflow task" in text
    assert "不得" in text


def test_advanced_docs_label_low_level_scripts_as_internal_diagnostics() -> None:
    for relative_path in ("ADVANCED_USER_GUIDE.md", "developer_guide.md", "scripts/README.md"):
        text = _repo_text(relative_path)
        assert "内部实现/诊断" in text
        assert "python scripts\\smt.py" in text


def test_ci_strict_contains_the_smt_public_entry_governance_check() -> None:
    source = _repo_text("scripts/ci_validate_repo.py")

    assert "def validate_smt_public_entry_contract(" in source
    assert "validate_smt_public_entry_contract(root, policy_payload, reporter)" in source


def test_smt_public_contract_tests_are_tracked_and_gitignore_is_precise() -> None:
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--error-unmatch",
            "tests/test_smt_cli.py",
            "tests/test_smt_cli_workspace.py",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr

    ignore_lines = {
        line.strip()
        for line in _repo_text(".gitignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {line for line in ignore_lines if line.startswith("!tests/")} == {
        "!tests/",
        "!tests/test_smt_cli.py",
        "!tests/test_smt_cli_workspace.py",
    }
    assert "tests/*" in ignore_lines


def test_pyproject_keeps_smt_cli_uninstalled_package_mode() -> None:
    pyproject = _repo_text("pyproject.toml")

    assert re.search(r"(?ms)^\[tool\.uv\]\s*\npackage\s*=\s*false\s*$", pyproject)
