from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ci_validate_repo  # noqa: E402


NORMATIVE_AGENT_SPEC = (
    Path("openspec")
    / "changes"
    / "add-smt-single-user-entry"
    / "specs"
    / "smt-public-cli"
    / "spec.md"
)


def test_normative_agent_spec_is_scanned_as_an_agent_cli_contract() -> None:
    assert NORMATIVE_AGENT_SPEC in ci_validate_repo.SMT_AGENT_CONTRACT_DOCS


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
