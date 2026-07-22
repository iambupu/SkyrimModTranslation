"""Regression tests for the stable SMT CLI result contract."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import get_args, get_type_hints

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import smt_cli  # noqa: E402
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


def test_duplicate_task_ids_fail_closed() -> None:
    assert smt_cli.select_exact_safe_task(
        _snapshot(tasks=[_task("same"), _task("same")]),
        "ExampleMod",
        datetime(2026, 7, 22, 12, 0, 0),
    ) is None


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
def test_multiple_current_blockers_do_not_bind_to_first_sorted_value(
    blockers: list[str],
) -> None:
    task = _task("repair", reason="chs_package_missing")
    snapshot = _snapshot(
        project_state="blocked",
        rows=[_state_row(state="blocked", blockers=blockers)],
        tasks=[task],
    )
    snapshot.policy["agent_orchestration_policy"]["auto_repair_allowed"] = blockers  # type: ignore[index]

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


def test_advance_uses_exact_resume_argv_and_stops_on_no_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    state = {"schema_version": 1, "generated_at": "local-time", "project_state": "candidates_extracted", "states": [_state_row()]}
    tasks = {"schema_version": 1, "tasks": [_task("task-42")]}
    session = _write_snapshot_files(workspace, state=state, tasks=tasks)
    policy_path = tmp_path / "workflow_policy.json"
    policy_path.write_text(json.dumps({"agent_orchestration_policy": {"max_same_blocker_attempts": 2}}), encoding="utf-8")
    runner = _RecordingRunner([0] * len(CORE_REFRESH_STEPS) + [0])
    services = smt_cli.SmtServices(runner=runner, policy_path=policy_path, max_steps=4)

    result = smt_cli.advance_workflow(workspace, session, services, 60)

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
    assert [row["status"] for row in attempt_rows] == ["started", "passed"]
    assert all(row["task_id"] == "task-42" for row in attempt_rows)
    assert all("state_digest" in row and "blocker" in row for row in attempt_rows)


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
