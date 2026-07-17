from __future__ import annotations

import hashlib
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

import aggregate_translation_projects  # noqa: E402
import used_capabilities  # noqa: E402
import validate_final_mod  # noqa: E402


def write_child(
    root: Path,
    name: str,
    *,
    relative: str,
    content: str,
    source: str,
    target: str,
    order: int,
    dependencies: list[str] | None = None,
    overrides: list[str] | None = None,
) -> None:
    child = root / "work" / "aggregate_inputs" / name
    overlay = child / "final_overlay" / Path(*relative.split("/"))
    overlay.parent.mkdir(parents=True)
    overlay.write_text(content, encoding="utf-8")
    (child / "manifest.json").write_text(
        json.dumps(
            {
                "project_name": name,
                "game_id": "skyrim-se",
                "status": "passed",
                "order": order,
                "dependencies": dependencies or [],
                "overrides": overrides or [],
            }
        ),
        encoding="utf-8",
    )
    (child / "provenance.jsonl").write_text(
        json.dumps(
            {
                "file": f"final_mod/{relative}",
                "file_sha256": hashlib.sha256(overlay.read_bytes()).hexdigest(),
                "game_id": "skyrim-se",
                "status": "assembled",
                "replaces_existing": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (child / "translation_dictionary.jsonl").write_text(
        json.dumps({"source": source, "target": target}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (child / "coverage.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "overlay_files": 1,
                "dictionary_entries": 1,
            }
        ),
        encoding="utf-8",
    )


def run_aggregate(root: Path, *, force: bool = False) -> int:
    environment = {
        "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }
    argv = ["aggregate_translation_projects.py", "--mod-name", "Mega"]
    if force:
        argv.append("--force")
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(sys, "argv", argv):
        return aggregate_translation_projects.main()


def test_aggregate_builds_valid_overlay_and_combined_dictionary(tmp_path: Path) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(json.dumps({"game_id": "skyrim-se"}), encoding="utf-8")
    write_child(
        tmp_path,
        "Core",
        relative="Interface/translations/core_english.txt",
        content="$HELLO\t你好\n",
        source="Hello",
        target="你好",
        order=0,
    )
    write_child(
        tmp_path,
        "Quests",
        relative="Interface/translations/quest_english.txt",
        content="$QUEST\t任务\n",
        source="Quest",
        target="任务",
        order=1,
    )
    assert run_aggregate(tmp_path) == 0
    final_mod = tmp_path / "out" / "Mega" / "汉化产出" / "final_mod"
    manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["AggregateProject"] is True
    assert manifest["DeliveryMode"] == "translation-overlay-package"
    assert manifest["TranslationDictionaryEntryCount"] == 2
    assert (tmp_path / "out" / "Mega" / "汉化产出" / "Mega_CHS.zip").is_file()

    environment = {
        "SKYRIM_CHS_WORKSPACE_ROOT": str(tmp_path),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
        sys,
        "argv",
        ["validate_final_mod.py", "--final-mod-dir", "out/Mega/汉化产出/final_mod"],
    ):
        assert validate_final_mod.main() == 0
    capability_report = used_capabilities.collect_used_capabilities(
        tmp_path,
        "Mega",
        final_mod,
    )
    assert capability_report["capabilities"][0]["name"] == "loose_text"


def test_aggregate_blocks_unresolved_path_and_dictionary_conflicts(tmp_path: Path) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(json.dumps({"game_id": "skyrim-se"}), encoding="utf-8")
    write_child(
        tmp_path,
        "Core",
        relative="Interface/translations/shared_english.txt",
        content="$HELLO\t你好\n",
        source="Hello",
        target="你好",
        order=0,
    )
    write_child(
        tmp_path,
        "Optional",
        relative="Interface/translations/shared_english.txt",
        content="$HELLO\t您好\n",
        source="Hello",
        target="您好",
        order=1,
    )
    with pytest.raises(ValueError, match="unresolved conflict"):
        run_aggregate(tmp_path)
    report = tmp_path / "out" / "Mega" / "aggregate" / "conflict_report.md"
    assert "Path conflict without declared override" in report.read_text(encoding="utf-8")
    assert "Dictionary conflict" in report.read_text(encoding="utf-8")
    assert not (tmp_path / "out" / "Mega" / "汉化产出" / "final_mod").exists()


def test_aggregate_uses_declared_order_and_validates_dependencies(tmp_path: Path) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(json.dumps({"game_id": "skyrim-se"}), encoding="utf-8")
    write_child(
        tmp_path,
        "ZCore",
        relative="Interface/translations/shared_english.txt",
        content="$HELLO\t你好\n",
        source="Hello",
        target="你好",
        order=0,
    )
    write_child(
        tmp_path,
        "AOverride",
        relative="Interface/translations/shared_english.txt",
        content="$HELLO\t您好\n",
        source="Greeting",
        target="您好",
        order=1,
        dependencies=["ZCore"],
        overrides=["ZCore"],
    )
    assert run_aggregate(tmp_path) == 0
    final_file = tmp_path / "out" / "Mega" / "汉化产出" / "final_mod" / "Interface" / "translations" / "shared_english.txt"
    assert final_file.read_text(encoding="utf-8") == "$HELLO\t您好\n"

    manifest_path = tmp_path / "work" / "aggregate_inputs" / "AOverride" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = ["Missing"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown dependencies"):
        run_aggregate(tmp_path, force=True)


def test_aggregate_blocks_binary_without_transferable_adapter_lineage(tmp_path: Path) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": "skyrim-se"}),
        encoding="utf-8",
    )
    write_child(
        tmp_path,
        "Scripts",
        relative="Scripts/Example.pex",
        content="binary fixture",
        source="Hello",
        target="你好",
        order=0,
    )

    with pytest.raises(ValueError, match="adapter lineage transfer is required"):
        run_aggregate(tmp_path)
    assert not (tmp_path / "out" / "Mega" / "汉化产出" / "final_mod").exists()
