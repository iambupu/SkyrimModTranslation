from __future__ import annotations

import struct

import pytest

from bethesda_advanced_fixture_builders import (
    build_fallout4_pex_call_occurrence_fixture,
    build_plugin_identity_fixture,
    build_string_table_fixture,
)


def test_plugin_identity_builder_encodes_full_and_light_form_ids() -> None:
    full = build_plugin_identity_fixture(
        owner_mod_key="FullMaster.esm",
        local_id=0x123456,
        master_style="full",
        full_index=0x02,
    )
    light = build_plugin_identity_fixture(
        owner_mod_key="LightMaster.esl",
        local_id=0x345,
        master_style="light",
        light_index=0x123,
    )

    assert full.raw_form_id == 0x02123456
    assert full.as_dict()["owner_mod_key"] == "FullMaster.esm"
    assert light.raw_form_id == 0xFE123345
    assert light.light_index == 0x123


@pytest.mark.parametrize(
    ("master_style", "local_id", "full_index", "light_index", "message"),
    [
        ("full", 0x1000000, 0, None, "24 bits"),
        ("full", 1, 0xFE, None, "0xFD"),
        ("light", 0x1000, None, 0, "12 bits"),
        ("light", 1, None, 0x1000, "12 bits"),
    ],
)
def test_plugin_identity_builder_rejects_out_of_range_components(
    master_style: str,
    local_id: int,
    full_index: int | None,
    light_index: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_plugin_identity_fixture(
            owner_mod_key="Fixture.esp",
            local_id=local_id,
            master_style=master_style,
            full_index=full_index,
            light_index=light_index,
        )


def test_fallout4_pex_occurrence_identity_ignores_translated_text() -> None:
    first = build_fallout4_pex_call_occurrence_fixture(
        object_name="FixtureScript",
        function_name="Run",
        instruction_index=4,
        opcode="CALLSTATIC",
        callee="Debug.Notification",
        argument_index=0,
        semantic_role="notification_text",
        source="Ready",
        classification="visible",
        visibility_basis="fallout4-visible-api-registry",
    )
    changed_text = build_fallout4_pex_call_occurrence_fixture(
        object_name="FixtureScript",
        function_name="Run",
        instruction_index=4,
        opcode="CALLSTATIC",
        callee="Debug.Notification",
        argument_index=0,
        semantic_role="notification_text",
        source="Different source text",
        classification="visible",
        visibility_basis="fallout4-visible-api-registry",
    )

    assert first.game_id == "fallout4"
    assert first.occurrence_id == changed_text.occurrence_id
    assert len(first.occurrence_id) == 24


@pytest.mark.parametrize("table_type", ["strings", "dlstrings", "ilstrings"])
def test_string_table_fixture_builder_writes_deterministic_header_and_directory(
    table_type: str,
) -> None:
    fixture = build_string_table_fixture(
        table_type,
        [(20, "Second"), (10, "First")],
    )

    count, data_size = struct.unpack_from("<II", fixture.payload, 0)
    first_id, first_offset = struct.unpack_from("<II", fixture.payload, 8)
    second_id, second_offset = struct.unpack_from("<II", fixture.payload, 16)
    data = fixture.payload[24:]

    assert fixture.entries == ((10, "First"), (20, "Second"))
    assert count == 2
    assert data_size == len(data)
    assert (first_id, first_offset) == (10, 0)
    assert second_id == 20
    if table_type == "strings":
        assert data == b"First\x00Second\x00"
        assert second_offset == len(b"First\x00")
    else:
        first_length = struct.unpack_from("<I", data, 0)[0]
        assert first_length == len(b"First\x00")
        assert data[4 : 4 + first_length] == b"First\x00"
        assert second_offset == 4 + first_length


def test_string_table_fixture_builder_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate string ID"):
        build_string_table_fixture("strings", [(1, "First"), (1, "Second")])
