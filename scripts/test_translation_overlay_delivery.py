from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_final_mod  # noqa: E402
import validate_final_mod  # noqa: E402
from game_context import load_game_profile  # noqa: E402


def prepare_build_inputs(root: Path, package_mode: str) -> dict[str, str]:
    (root / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": "skyrim-se"}),
        encoding="utf-8",
    )
    source = root / "work" / "extracted_mods" / "Fixture"
    original = source / "Interface" / "translations" / "fixture_english.txt"
    original.parent.mkdir(parents=True)
    original.write_text("$HELLO\tHello\n", encoding="utf-8")
    dictionary = root / "translated" / "text_assets" / "Fixture" / "dictionary.jsonl"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    qa = root / "qa"
    qa.mkdir()
    (qa / "Fixture.scale_execution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "mod-scale-execution",
                "mod_name": "Fixture",
                "game_id": "skyrim-se",
                "status": "ready",
                "scale_level": "L4" if package_mode != "aggregate-only" else "L5",
                "effective": {"package_mode": package_mode},
            }
        ),
        encoding="utf-8",
    )
    return {
        "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }


def promoted_string_table_context():
    context = load_game_profile("skyrim-se")
    capabilities = dict(context.capabilities)
    capabilities["string_tables"] = replace(
        capabilities["string_tables"],
        level="stable",
    )
    return replace(context, capabilities=MappingProxyType(capabilities))


def promoted_localized_delivery_context():
    context = load_game_profile("skyrim-se")
    capabilities = dict(context.capabilities)
    capabilities["localized_delivery"] = replace(
        capabilities["localized_delivery"],
        level="stable",
    )
    return replace(context, capabilities=MappingProxyType(capabilities))


def test_controlled_string_table_adds_only_profile_mapped_target_file(
    tmp_path: Path,
) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": "skyrim-se"}),
        encoding="utf-8",
    )
    source = tmp_path / "work" / "extracted_mods" / "Fixture"
    source_table = source / "Strings" / "Example_english.strings"
    source_table.parent.mkdir(parents=True)
    source_table.write_bytes(b"source-table")
    output_table = (
        tmp_path
        / "out"
        / "Fixture"
        / "tool_outputs"
        / "Strings"
        / "Example_chinese.strings"
    )
    output_table.parent.mkdir(parents=True)
    output_table.write_bytes(b"translated-table")
    dictionary = tmp_path / "translated" / "text_assets" / "Fixture" / "dictionary.jsonl"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    environment = {
        "SKYRIM_CHS_WORKSPACE_ROOT": str(tmp_path),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        build_final_mod,
        "current_game_context",
        return_value=promoted_string_table_context(),
    ), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "work/extracted_mods/Fixture",
            "--force",
        ],
    ):
        assert build_final_mod.main() == 0

    final_mod = tmp_path / "out" / "Fixture" / "汉化产出" / "final_mod"
    assert (final_mod / "Strings" / "Example_english.strings").read_bytes() == b"source-table"
    assert (final_mod / "Strings" / "Example_chinese.strings").read_bytes() == b"translated-table"
    rows = [
        json.loads(line)
        for line in (final_mod / "meta" / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target = next(
        row for row in rows if row["file"] == "final_mod/Strings/Example_chinese.strings"
    )
    assert target["transform"] == "controlled-string-table-output"
    assert target["tool"] == "BethesdaStringTableTool"
    assert target["string_table_source"].replace("\\", "/").endswith(
        "Strings/Example_english.strings"
    )


def test_scale_auto_mode_builds_valid_translation_overlay(tmp_path: Path) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": "skyrim-se"}),
        encoding="utf-8",
    )
    source = tmp_path / "work" / "extracted_mods" / "Fixture"
    original = source / "Interface" / "translations" / "fixture_english.txt"
    protected = source / "Textures" / "large.dds"
    original.parent.mkdir(parents=True)
    protected.parent.mkdir(parents=True)
    original.write_text("$HELLO\tHello\n", encoding="utf-8")
    protected.write_bytes(b"do-not-package")

    overlay = tmp_path / "translated" / "final_mod" / "Fixture" / "Interface" / "translations" / "fixture_english.txt"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("$HELLO\t你好\n", encoding="utf-8")
    dictionary = tmp_path / "translated" / "text_assets" / "Fixture" / "dictionary.jsonl"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    scale_report = tmp_path / "qa" / "Fixture.scale_execution.json"
    scale_report.parent.mkdir(parents=True)
    scale_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                    "report_type": "mod-scale-execution",
                    "mod_name": "Fixture",
                    "game_id": "skyrim-se",
                    "status": "ready",
                "scale_level": "L4",
                "effective": {"package_mode": "translation-overlay"},
            }
        ),
        encoding="utf-8",
    )

    environment = {
        "SKYRIM_CHS_WORKSPACE_ROOT": str(tmp_path),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "work/extracted_mods/Fixture",
            "--force",
        ],
    ):
        assert build_final_mod.main() == 0

    final_mod = tmp_path / "out" / "Fixture" / "汉化产出" / "final_mod"
    manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["DeliveryMode"] == "translation-overlay-package"
    assert manifest["RequiresOriginalMod"] is True
    assert manifest["IncludesOriginalFiles"] is False
    assert len(manifest["ReplacementFilesApplied"]) == 1
    assert not (final_mod / "Textures" / "large.dds").exists()

    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "validate_final_mod.py",
            "--final-mod-dir",
            "out/Fixture/汉化产出/final_mod",
        ],
    ):
        assert validate_final_mod.main() == 0


def test_localized_string_table_requires_verified_composite_receipt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "work" / "extracted_mods" / "Fixture"
    source_table = source / "Strings" / "Example_english.strings"
    output_table = tmp_path / "out" / "Fixture" / "tool_outputs" / "Strings" / "Example_chinese.strings"
    plugin = source / "Example.esp"
    source_table.parent.mkdir(parents=True)
    output_table.parent.mkdir(parents=True)
    source_table.write_bytes(b"source-table")
    output_table.write_bytes(b"target-table")
    plugin.write_bytes(
        b"TES4"
        + (0).to_bytes(4, "little")
        + (0x00000080).to_bytes(4, "little")
        + b"\0" * 12
    )

    with pytest.raises(ValueError, match="no verified composite receipt"):
        build_final_mod.validate_localized_delivery_for_output(
            root=tmp_path,
            source=source,
            safe_mod_name="Fixture",
            output_file=output_table,
            source_relative=Path("Strings/Example_english.strings"),
            context=promoted_localized_delivery_context(),
        )


@pytest.mark.parametrize(
    ("package_mode", "requested_mode", "expected"),
    [
        ("translation-overlay", "complete", "conflicts with scale execution"),
        ("aggregate-only", "complete", "requires aggregate-only delivery"),
    ],
)
def test_explicit_delivery_mode_cannot_bypass_scale_policy(
    tmp_path: Path,
    package_mode: str,
    requested_mode: str,
    expected: str,
) -> None:
    environment = prepare_build_inputs(tmp_path, package_mode)
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "work/extracted_mods/Fixture",
            "--delivery-mode",
            requested_mode,
        ],
    ), pytest.raises(ValueError, match=expected):
        build_final_mod.main()


def test_translation_overlay_requires_scale_execution_evidence(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "translation-overlay")
    (tmp_path / "qa" / "Fixture.scale_execution.json").unlink()
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "work/extracted_mods/Fixture",
            "--delivery-mode",
            "translation-overlay",
        ],
    ), pytest.raises(ValueError, match="requires a ready scale execution report"):
        build_final_mod.main()


def test_final_mod_transaction_restores_previous_outputs_after_failure(
    tmp_path: Path,
) -> None:
    final_mod = tmp_path / "out" / "Fixture" / "final_mod"
    intermediate = tmp_path / "out" / "Fixture" / "intermediate"
    package = tmp_path / "out" / "Fixture" / "Fixture_CHS.zip"
    report = tmp_path / "out" / "Fixture" / "package_report.md"
    for path, value in (
        (final_mod / "old.txt", b"old-final"),
        (intermediate / "old.txt", b"old-intermediate"),
        (package, b"old-package"),
        (report, b"old-report"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    transaction = build_final_mod.FinalModBuildTransaction(
        final_mod,
        (intermediate, package, report),
    )
    staging = transaction.begin()
    (staging / "new.txt").write_bytes(b"new-final")
    transaction.publish()
    intermediate.mkdir(parents=True)
    (intermediate / "new.txt").write_bytes(b"new-intermediate")
    package.write_bytes(b"new-package")
    report.write_bytes(b"new-report")

    transaction.rollback()

    assert (final_mod / "old.txt").read_bytes() == b"old-final"
    assert (intermediate / "old.txt").read_bytes() == b"old-intermediate"
    assert package.read_bytes() == b"old-package"
    assert report.read_bytes() == b"old-report"
    assert not list(final_mod.parent.glob(".*.backup"))
    assert not list(final_mod.parent.glob(".*.tmp"))


def test_final_mod_transaction_commit_keeps_new_output_without_residue(
    tmp_path: Path,
) -> None:
    final_mod = tmp_path / "out" / "Fixture" / "final_mod"
    (final_mod / "old.txt").parent.mkdir(parents=True)
    (final_mod / "old.txt").write_bytes(b"old")
    transaction = build_final_mod.FinalModBuildTransaction(final_mod, ())
    staging = transaction.begin()
    (staging / "new.txt").write_bytes(b"new")

    transaction.publish()
    transaction.commit()

    assert (final_mod / "new.txt").read_bytes() == b"new"
    assert not (final_mod / "old.txt").exists()
    assert not list(final_mod.parent.glob(".*.backup"))
    assert not list(final_mod.parent.glob(".*.tmp"))
