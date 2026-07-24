from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ci_validate_repo  # noqa: E402
import test_workflow_health  # noqa: E402
import verify_python_runtime_lock  # noqa: E402


def test_normative_agent_contract_sources_are_tracked_runtime_inputs() -> None:
    assert ci_validate_repo.SMT_NORMATIVE_AGENT_CONTRACT_SOURCES == (
        (Path("AGENTS.md"), "唯一公开 CLI 入口"),
        (
            Path("skills") / "skyrim-mod-chs-translation" / "SKILL.md",
            None,
        ),
    )
    assert ci_validate_repo.SMT_AGENT_CONTRACT_DOCS == tuple(
        source
        for source, _heading in ci_validate_repo.SMT_NORMATIVE_AGENT_CONTRACT_SOURCES
    )
    assert "validate_readme_links" not in ci_validate_repo.main.__code__.co_names
    assert not hasattr(ci_validate_repo, "NON_WINDOWS_FENCE_RE")


def test_workflow_health_policy_scan_excludes_ordinary_documentation() -> None:
    relative_paths = {
        path.relative_to(ROOT).as_posix()
        for path in test_workflow_health.iter_policy_files(ROOT)
    }

    assert "AGENTS.md" in relative_paths
    assert "skills/skyrim-mod-chs-translation/SKILL.md" in relative_paths
    assert "README.md" not in relative_paths
    assert "USER_GUIDE.md" not in relative_paths
    assert "ADVANCED_USER_GUIDE.md" not in relative_paths
    assert "developer_guide.md" not in relative_paths
    assert not any(path.startswith("docs/") for path in relative_paths)
    assert "tools/README.md" not in relative_paths
    assert "scripts/README.md" not in relative_paths


def test_ci_workflow_does_not_run_documentation_contract_suite() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "test_task6b2_documentation.py" not in workflow
    assert "documentation contracts" not in workflow


def test_ci_routes_win32_managed_tool_regressions_to_windows_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "--ignore-glob=tests/test_managed_tool_*.py" in workflow
    assert "--ignore=tests/test_manage_managed_tool_cache.py" in workflow
    assert "Run Windows shared managed-tool regressions" in workflow
    for relative_path in (
        "tests/test_managed_tool_store.py",
        "tests/test_managed_tool_concurrency.py",
        "tests/test_managed_tool_provisioning.py",
        "tests/test_managed_tool_migration.py",
        "tests/test_managed_tool_maintenance.py",
        "tests/test_manage_managed_tool_cache.py",
        "tests/test_plugin_install_and_tool_setup.py",
    ):
        assert relative_path in workflow


def test_runtime_lock_digest_is_independent_of_checkout_line_endings(
    tmp_path: Path,
) -> None:
    lf_path = tmp_path / "lf.lock"
    crlf_path = tmp_path / "crlf.lock"
    lf_path.write_bytes(b"version = 1\nrevision = 3\n")
    crlf_path.write_bytes(b"version = 1\r\nrevision = 3\r\n")

    assert verify_python_runtime_lock._sha256_text(lf_path) == (
        verify_python_runtime_lock._sha256_text(crlf_path)
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "USER_GUIDE.md",
        "ADVANCED_USER_GUIDE.md",
        "developer_guide.md",
        "docs/agent_workflow.md",
        "scripts/README.md",
        "tools/README.md",
        "agents/opencode/README.md",
    ],
)
def test_ci_text_scans_exclude_ordinary_documentation(relative_path: str) -> None:
    assert ci_validate_repo.is_ci_checked_text_source(Path(relative_path)) is False


@pytest.mark.parametrize(
    "relative_path",
    [
        "AGENTS.md",
        "skills/skyrim-mod-chs-translation/SKILL.md",
        "skills/workflow-policy-and-state/SKILL.md",
        "agents/opencode/prompt.md",
        "scripts/smt_cli.py",
        "config/workflow_policy.json",
    ],
)
def test_ci_text_scans_keep_agent_contract_and_code_assets(relative_path: str) -> None:
    assert ci_validate_repo.is_ci_checked_text_source(Path(relative_path)) is True


@pytest.mark.parametrize(
    "text",
    [
        (
            "顶层 Agent 不得调用 `run --format json`，也不得调用 "
            "`resume --format json`。"
        ),
        "顶层 Agent 不得把 `run --format json` 当成有效入口。",
        "顶层 Agent 不得将 `resume --format json` 视为有效入口。",
    ],
)
def test_misordered_smt_commands_ignore_explicitly_forbidden_examples(
    text: str,
) -> None:
    assert ci_validate_repo.misordered_smt_json_commands(text) == []


@pytest.mark.parametrize(
    ("contract", "expected_error"),
    [
        (
            "顶层 Agent MUST 首次调用 `run --format json`，后续仅调用 "
            "`resume --format json`、`status --format json`、"
            "`doctor --format json` 和 `output --format json`。",
            "misordered_commands",
        ),
        (
            "顶层 Agent MUST 首次调用 "
            "`python scripts\\smt.py --format json run`；后续仅调用 "
            "`python scripts\\smt.py --format json resume`、"
            "`python scripts\\smt.py --format json status` 和 "
            "`python scripts\\smt.py --format json output`。",
            "missing_commands=['doctor']",
        ),
    ],
)
def test_normative_agent_contract_rejects_bad_order_or_missing_command(
    contract: str,
    expected_error: str,
) -> None:
    errors = ci_validate_repo.smt_normative_agent_contract_errors(contract)

    assert any(expected_error in error for error in errors), errors


def test_normative_agent_contract_accepts_complete_commands_and_negated_examples() -> None:
    contract = (
        "顶层 Agent 不得调用 `run --format json` 或 `resume --format json`。"
        "顶层 Agent MUST 首次调用 "
        "`python scripts\\smt.py --format json run`；后续仅调用 "
        "`python scripts\\smt.py --format json resume`、"
        "`python scripts\\smt.py --format json status`、"
        "`python scripts\\smt.py --format json doctor` 和 "
        "`python scripts\\smt.py --format json output`。"
    )

    assert ci_validate_repo.smt_normative_agent_contract_errors(contract) == []


def test_managed_tool_maintenance_contract_is_static_and_not_authorized() -> None:
    reporter = ci_validate_repo.Reporter()
    policy = ci_validate_repo.load_json_file(
        ROOT,
        ci_validate_repo.WORKFLOW_POLICY_JSON,
        reporter,
    )

    ci_validate_repo.validate_managed_tool_maintenance_contract(
        ROOT,
        policy,
        reporter,
    )

    assert reporter.failed == []
