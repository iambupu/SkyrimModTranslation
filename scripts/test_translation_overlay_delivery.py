from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_final_mod  # noqa: E402
import validate_final_mod  # noqa: E402


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
