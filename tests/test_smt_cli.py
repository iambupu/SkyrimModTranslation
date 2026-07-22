"""Regression tests for the stable SMT CLI result contract."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import get_args, get_type_hints


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SMT_CLI_PATH = REPOSITORY_ROOT / "scripts" / "smt_cli.py"
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


def load_smt_cli():
    spec = importlib.util.spec_from_file_location("smt_cli", SMT_CLI_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_empty_result_has_the_complete_v1_payload_and_is_json_serializable() -> None:
    smt_cli = load_smt_cli()

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
    smt_cli = load_smt_cli()

    result = smt_cli.empty_result("status")
    result.workspace = r"D:\mods\Example Workspace"
    payload = result.to_payload()

    assert payload["workspace"] == r"D:\mods\Example Workspace"
    assert json.loads(json.dumps(payload))["workspace"] == r"D:\mods\Example Workspace"


def test_next_action_artifacts_follow_the_public_artifact_structure() -> None:
    smt_cli = load_smt_cli()

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
    smt_cli = load_smt_cli()

    assert smt_cli.EXIT_SUCCESS == 0
    assert smt_cli.EXIT_INTERNAL_READ_OR_BUSY == 1
    assert smt_cli.EXIT_SAFE_STOP == 3
    assert smt_cli.EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY == 4
    assert smt_cli.EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE == 5
    assert smt_cli.EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT == 6
    assert smt_cli.EXIT_TIMEOUT == 124
    assert smt_cli.EXIT_INTERRUPTED == 130


def test_module_import_and_empty_result_do_not_write_to_standard_streams() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy; "
                f"module = runpy.run_path({str(SMT_CLI_PATH)!r}); "
                "module['empty_result']('status')"
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
