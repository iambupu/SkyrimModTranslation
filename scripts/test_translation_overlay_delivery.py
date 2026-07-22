from __future__ import annotations

import json
import os
import sys
import zipfile
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


def test_translation_dictionary_rejects_malformed_jsonl(tmp_path: Path) -> None:
    source = tmp_path / "translated" / "text_assets" / "Fixture" / "dictionary.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"source":"Hello","target":"你好"}\n{invalid}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"dictionary\.jsonl:2"):
        build_final_mod.jsonl_dictionary_entries(tmp_path, source)


def test_translation_dictionary_rejects_malformed_xml(tmp_path: Path) -> None:
    source = tmp_path / "translated" / "xtranslator_ready" / "Fixture" / "broken.xml"
    source.parent.mkdir(parents=True)
    source.write_text("<SSTXMLRessources>", encoding="utf-8")

    with pytest.raises(ValueError, match="broken.xml"):
        build_final_mod.xml_dictionary_entries(tmp_path, source)


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


def test_final_mod_build_rejects_hardlinked_prepared_source(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "complete")
    source_file = (
        tmp_path
        / "work"
        / "extracted_mods"
        / "Fixture"
        / "Interface"
        / "translations"
        / "fixture_english.txt"
    )
    outside = tmp_path / "outside-interface.txt"
    outside.write_text("$HELLO\tOutside\n", encoding="utf-8")
    source_file.unlink()
    os.link(outside, source_file)

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
    ), pytest.raises(ValueError, match="hardlink|multiple hardlinks"):
        build_final_mod.main()


def test_final_mod_build_rejects_source_zip_over_scale_limit(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "complete")
    scale_path = tmp_path / "qa" / "Fixture.scale_execution.json"
    scale = json.loads(scale_path.read_text(encoding="utf-8"))
    scale["effective"].update(
        {
            "max_files": 10,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "timeout_seconds": 60,
        }
    )
    scale_path.write_text(json.dumps(scale), encoding="utf-8")
    source_zip = tmp_path / "mod" / "Fixture.zip"
    source_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(source_zip, "w") as archive:
        archive.writestr("Interface/translations/fixture_english.txt", b"A" * 2048)

    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "mod/Fixture.zip",
            "--force",
        ],
    ), pytest.raises(ValueError, match="max_file_bytes"):
        build_final_mod.main()


def test_final_mod_build_rejects_hardlinked_source_zip(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "complete")
    outside = tmp_path / "outside.zip"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("Interface/translations/fixture_english.txt", "$HELLO\tHello\n")
    source_zip = tmp_path / "mod" / "Fixture.zip"
    source_zip.parent.mkdir(parents=True)
    os.link(outside, source_zip)

    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "mod/Fixture.zip",
            "--force",
        ],
    ), pytest.raises(ValueError, match="hardlink|multiple hardlinks"):
        build_final_mod.main()


def test_final_mod_build_rejects_source_zip_drift_after_copy(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "complete")
    source_zip = tmp_path / "mod" / "Fixture.zip"
    source_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(source_zip, "w") as archive:
        archive.writestr(
            "Interface/translations/fixture_english.txt",
            "$HELLO\tHello\n",
        )

    real_sha256 = build_final_mod.sha256_file
    zip_hash_calls = 0

    def drifting_sha256(path: Path) -> str:
        nonlocal zip_hash_calls
        if Path(path).resolve(strict=False) == source_zip.resolve(strict=False):
            zip_hash_calls += 1
            if zip_hash_calls >= 3:
                return "0" * 64
        return real_sha256(path)

    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        build_final_mod,
        "sha256_file",
        side_effect=drifting_sha256,
    ), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "mod/Fixture.zip",
            "--force",
        ],
    ), pytest.raises(RuntimeError, match="Source ZIP changed during final assembly copy"):
        build_final_mod.main()

    assert zip_hash_calls == 3
    assert not (
        tmp_path / "out" / "Fixture" / "汉化产出" / "final_mod"
    ).exists()


def test_final_mod_build_treats_zip_suffixed_directory_as_directory(tmp_path: Path) -> None:
    environment = prepare_build_inputs(tmp_path, "complete")
    source = tmp_path / "mod" / "Fixture.zip"
    visible = source / "Interface" / "translations" / "fixture_english.txt"
    visible.parent.mkdir(parents=True)
    visible.write_text("$HELLO\tHello\n", encoding="utf-8")

    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        [
            "build_final_mod.py",
            "--mod-name",
            "Fixture",
            "--source-mod-dir",
            "mod/Fixture.zip",
            "--force",
        ],
    ):
        assert build_final_mod.main() == 0

    assert (
        tmp_path
        / "out"
        / "Fixture"
        / "汉化产出"
        / "final_mod"
        / "Interface"
        / "translations"
        / "fixture_english.txt"
    ).is_file()


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
