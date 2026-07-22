from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bethesda_advanced_fixture_builders import (  # noqa: E402
    build_fallout4_pex_call_occurrence_fixture,
)
from audit_pex_delivery import translated_row_summary  # noqa: E402
from pex_visible_api_registry import load_pex_visible_api_registry  # noqa: E402
from invoke_mutagen_pex_string_tool import (  # noqa: E402
    validate_semantic_pex_jsonl,
)
from new_model_review_packet import collect_rows  # noqa: E402
from pex_translation_safety import (  # noqa: E402
    pex_row_is_writable_candidate,
    pex_translation_skip_reason,
)


REGISTRY_PATH = ROOT / "config" / "pex_visible_apis" / "fallout4.json"


def write_registry(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "fallout4.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def semantic_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 2,
        "game_id": "fallout4",
        "ModName": "Semantic.pex",
        "Type": "PEX",
        "Source": "Player visible text",
        "Result": "",
        "risk": "candidate",
        "object_name": "SemanticFixture",
        "state_name": "",
        "function_name": "Run",
        "opcode": "CALLSTATIC",
        "opcode_form": "CALLSTATIC",
        "instruction_index": 0,
        "argument_index": 4,
        "callee": "Debug.Notification",
        "semantic_argument_index": 0,
        "semantic_argument_role": "notification_text",
        "visibility_basis": (
            "registry:fallout4.json#Debug.Notification[0]/"
            "fixture:debug-notification-direct-literal"
        ),
        "classification": "visible",
        "value_kind": "String",
        "is_direct_literal": True,
        "notes": "",
    }
    row.update(overrides)
    return row


def test_fallout4_registry_is_fixture_backed() -> None:
    registry = load_pex_visible_api_registry(
        REGISTRY_PATH,
        expected_game_id="fallout4",
    )
    fixtures = []
    for api_index, api in enumerate(registry["apis"]):
        assert api["evidence"]
        for argument in api["arguments"]:
            fixtures.append(
                build_fallout4_pex_call_occurrence_fixture(
                    object_name="RegistryFixture",
                    function_name=f"Api{api_index}",
                    instruction_index=api_index,
                    opcode=api["opcode_forms"][0],
                    callee=api["callee"],
                    argument_index=argument["index"],
                    semantic_role=argument["semantic_role"],
                    source=f"fixture text {api_index}-{argument['index']}",
                    classification=argument["classification"],
                    visibility_basis=api["evidence"][0],
                )
            )

    assert {fixture.classification for fixture in fixtures} == {"visible", "protected"}
    assert all(fixture.game_id == "fallout4" for fixture in fixtures)
    assert len({fixture.occurrence_id for fixture in fixtures}) == len(fixtures)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda payload: payload.update(unmatched_classification="visible"),
            "unmatched_classification must be manual_review",
        ),
        (
            lambda payload: payload["apis"].append(copy.deepcopy(payload["apis"][0])),
            "duplicate PEX visible API callee",
        ),
        (
            lambda payload: payload["apis"][0]["arguments"].append(
                copy.deepcopy(payload["apis"][0]["arguments"][0])
            ),
            "duplicate index",
        ),
        (
            lambda payload: payload["apis"][0].update(opcode_forms=["ASSIGN"]),
            "unsupported values",
        ),
        (
            lambda payload: payload["apis"][0].update(evidence=[]),
            "evidence must be a non-empty array",
        ),
    ],
)
def test_registry_rejects_unsafe_schema(
    tmp_path: Path,
    mutate,
    expected: str,
) -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    mutate(payload)

    with pytest.raises(ValueError, match=expected):
        load_pex_visible_api_registry(
            write_registry(tmp_path, payload),
            expected_game_id="fallout4",
        )


def test_semantic_classification_cannot_be_promoted_by_natural_language_context() -> None:
    protected = semantic_row(
        Source="This looks like a player notification",
        Result="不允许写回",
        risk="candidate",
        classification="manual_review",
        is_direct_literal=False,
        notes="confirmed visible notification",
    )
    assert not pex_row_is_writable_candidate(protected)
    assert "semantic classification" in pex_translation_skip_reason(protected)

    visible = semantic_row(Source="Scripts/example.pex", Result="显示的脚本路径")
    assert pex_row_is_writable_candidate(visible)
    assert pex_translation_skip_reason(visible) == ""


def test_wrapper_preflight_rejects_non_visible_write_rows(tmp_path: Path) -> None:
    valid = tmp_path / "valid.jsonl"
    valid.write_text(
        json.dumps(semantic_row(Result="玩家可见文本"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    validate_semantic_pex_jsonl(valid, expected_game_id="fallout4")

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(
        json.dumps(
            semantic_row(
                Result="错误写回",
                classification="protected",
                risk="candidate",
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="protected Fallout 4 PEX rows are not writable"):
        validate_semantic_pex_jsonl(invalid, expected_game_id="fallout4")


def test_model_review_packet_excludes_untranslated_non_visible_semantic_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "semantic.jsonl"
    source.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in (
                semantic_row(),
                semantic_row(
                    Source="Diagnostic text",
                    risk="protected-logic",
                    classification="protected",
                    callee="Debug.Trace",
                    semantic_argument_role="diagnostic_text",
                ),
                semantic_row(
                    Source="::dynamicMessage",
                    risk="manual-review",
                    classification="manual_review",
                    is_direct_literal=False,
                ),
            )
        ),
        encoding="utf-8",
    )

    rows = collect_rows(tmp_path, [source], include_protected_rows=False)
    assert [row["Source"] for row in rows] == ["Player visible text"]


def test_pex_delivery_audit_blocks_translated_non_visible_semantic_rows() -> None:
    visible = semantic_row(Result="可见译文")
    protected = semantic_row(
        Result="不允许的译文",
        classification="protected",
    )

    translated, unauthorized = translated_row_summary([visible, protected])

    assert translated == 1
    assert len(unauthorized) == 1
    assert "semantic classification" in unauthorized[0]
