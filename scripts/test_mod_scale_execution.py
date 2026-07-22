from __future__ import annotations

import json
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from game_context import load_game_profile  # noqa: E402
import mod_materialization  # noqa: E402
from mod_materialization import materialize_source  # noqa: E402
from mod_scale_policy import ScaleExecutionPolicy, resolve_scale_execution_policy  # noqa: E402


CONFIG_PATH = ROOT / "config" / "mod_scale_profiles.json"


def write_assessment(root: Path, *, level: str = "L2", unpacked: int = 100, protected: int = 0) -> Path:
    path = root / "qa" / "Fixture.scale_assessment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "mod-scale-assessment",
                "mod_name": "Fixture",
                "game_id": "skyrim-se",
                "scale_level": level,
                "risk_level": "R0",
                "estimated_unpacked_bytes": unpacked,
                "protected_bytes": protected,
                "compressed_bytes": 0,
                "file_count": 2,
                "largest_file_bytes": unpacked,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_policy_records_overrides_and_enforces_absolute_caps(tmp_path: Path) -> None:
    assessment = write_assessment(tmp_path)
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    output.parent.mkdir(parents=True)
    with mock.patch("mod_scale_policy.shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)):
        policy, report = resolve_scale_execution_policy(
            root=tmp_path,
            mod_name="Fixture",
            assessment_path=assessment,
            config_path=CONFIG_PATH,
            output_path=output,
            overrides={"max_files": 80000, "extract_mode": "selective"},
        )
    assert policy.limits["max_files"] == 80000
    assert report["overrides"] == {"max_files": 80000, "extract_mode": "selective"}
    assert report["disk_preflight"]["passed"] is True

    with mock.patch("mod_scale_policy.shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)):
        with pytest.raises(ValueError, match="absolute safety cap"):
            resolve_scale_execution_policy(
                root=tmp_path,
                mod_name="Fixture",
                assessment_path=assessment,
                config_path=CONFIG_PATH,
                output_path=output,
                overrides={"max_files": 1_000_001},
            )
        with pytest.raises(ValueError, match="translation_batch_rows"):
            resolve_scale_execution_policy(
                root=tmp_path,
                mod_name="Fixture",
                assessment_path=assessment,
                config_path=CONFIG_PATH,
                output_path=output,
                overrides={"translation_batch_rows": 100_001},
            )


def test_policy_blocks_insufficient_disk_and_l5(tmp_path: Path) -> None:
    assessment = write_assessment(tmp_path, unpacked=1024**3)
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    output.parent.mkdir(parents=True)
    with mock.patch("mod_scale_policy.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
        with pytest.raises(ValueError, match="Insufficient disk space"):
            resolve_scale_execution_policy(
                root=tmp_path,
                mod_name="Fixture",
                assessment_path=assessment,
                config_path=CONFIG_PATH,
                output_path=output,
                overrides={"extract_mode": "selective"},
            )

    write_assessment(tmp_path, level="L5")
    with mock.patch("mod_scale_policy.shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)):
        with pytest.raises(ValueError, match="split into independent translation workspaces"):
            resolve_scale_execution_policy(
                root=tmp_path,
                mod_name="Fixture",
                assessment_path=assessment,
                config_path=CONFIG_PATH,
                output_path=output,
            )


def test_policy_rejects_cross_game_assessment(tmp_path: Path) -> None:
    assessment = write_assessment(tmp_path)
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    output.parent.mkdir(parents=True)
    with pytest.raises(ValueError, match="game_id"):
        resolve_scale_execution_policy(
            root=tmp_path,
            mod_name="Fixture",
            assessment_path=assessment,
            config_path=CONFIG_PATH,
            output_path=output,
            expected_game_id="fallout4",
        )


def policy_for(tmp_path: Path, *, max_files: int = 100) -> ScaleExecutionPolicy:
    return ScaleExecutionPolicy(
        scale_level="L3",
        risk_level="R1",
        extract_mode="selective",
        package_mode="translation-overlay",
        limits={
            "max_files": max_files,
            "max_file_bytes": 1024 * 1024,
            "max_total_bytes": 8 * 1024 * 1024,
            "timeout_seconds": 60,
            "max_parallel_tasks": 2,
            "max_parallel_binary_tasks": 1,
            "max_parallel_archive_tasks": 1,
        },
        overrides={},
        checkpoint_every_files=1,
        translation_batch_rows=1000,
        estimated_materialized_bytes=1024,
        required_free_bytes=1024,
        available_free_bytes=1024**3,
        refresh_existing_output=False,
        config_path=CONFIG_PATH,
        assessment_path=tmp_path / "qa" / "Fixture.scale_assessment.json",
    )


def test_selective_directory_materialization_resumes_unchanged_shards(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture"
    translated = source / "Interface" / "translations" / "fixture_en.txt"
    protected = source / "Textures" / "fixture.dds"
    translated.parent.mkdir(parents=True)
    protected.parent.mkdir(parents=True)
    translated.write_text("$HELLO\tHello\n", encoding="utf-8")
    protected.write_bytes(b"texture")
    output = tmp_path / "work" / "extracted_mods" / "Fixture"

    first = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert first.materialized_files == 1
    assert not (output / "Textures" / "fixture.dds").exists()
    assert (output / "Interface" / "translations" / "fixture_en.txt").is_file()

    second = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert second.reused_files == 1
    assert second.materialized_files == 0

    translated.write_text("$HELLO\tChanged\n", encoding="utf-8")
    third = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert third.reused_files == 0
    assert third.materialized_files == 1
    index = json.loads((tmp_path / "work" / "shards" / "Fixture" / "index.json").read_text(encoding="utf-8"))
    assert index["selected_files"] == 1
    translated_shard = next(
        shard
        for shard in index["shards"]
        if shard["relative_path"] == "Interface/translations/fixture_en.txt"
    )
    assert translated_shard["status"] == "materialized"

    translated.unlink()
    fourth = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert fourth.reused_files == 0
    assert fourth.materialized_files == 0
    assert not (output / "Interface" / "translations" / "fixture_en.txt").exists()
    index = json.loads((tmp_path / "work" / "shards" / "Fixture" / "index.json").read_text(encoding="utf-8"))
    assert index["removed_stale_files"] == 1


def test_materialization_exact_file_limit_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    with pytest.raises(ValueError, match="selected file count"):
        materialize_source(
            root=tmp_path,
            mod_name="Fixture",
            source=source,
            output_dir=tmp_path / "work" / "extracted_mods" / "Fixture",
            context=load_game_profile("skyrim-se"),
            policy=policy_for(tmp_path, max_files=1),
            force=False,
            resume=False,
        )


def test_zip_materialization_skips_links_and_blocks_duplicate_windows_paths(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Interface/translations/fixture_en.txt", "$HELLO\tHello\n")
        link = zipfile.ZipInfo("Interface/translations/link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "fixture_en.txt")
    result = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=tmp_path / "work" / "extracted_mods" / "Fixture",
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert result.materialized_files == 1
    assert any("ZIP link entry blocked" in value for value in result.skipped_entries)
    assert not (result.output_dir / "Interface" / "translations" / "link.txt").exists()

    duplicate = tmp_path / "mod" / "Duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("Interface/A.txt", "A")
        archive.writestr("interface/a.TXT", "B")
    with pytest.raises(ValueError, match="duplicate Windows-equivalent path"):
        materialize_source(
            root=tmp_path,
            mod_name="Duplicate",
            source=duplicate,
            output_dir=tmp_path / "work" / "extracted_mods" / "Duplicate",
            context=load_game_profile("skyrim-se"),
            policy=policy_for(tmp_path),
            force=False,
            resume=True,
        )


def test_zip_materialization_rejects_archive_replaced_after_inventory(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture.zip"
    source.parent.mkdir(parents=True)
    member_name = "Interface/translations/fixture_en.txt"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member_name, "A")

    real_write_plan = mod_materialization._write_inventory_and_plan

    def replace_archive_after_inventory(**kwargs) -> None:
        real_write_plan(**kwargs)
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(member_name, b"B" * (2 * 1024 * 1024))

    with mock.patch.object(
        mod_materialization,
        "_write_inventory_and_plan",
        side_effect=replace_archive_after_inventory,
    ):
        with pytest.raises(RuntimeError, match="changed after inventory"):
            materialize_source(
                root=tmp_path,
                mod_name="Fixture",
                source=source,
                output_dir=tmp_path / "work" / "extracted_mods" / "Fixture",
                context=load_game_profile("skyrim-se"),
                policy=policy_for(tmp_path),
                force=False,
                resume=True,
            )


def test_interrupted_materialization_reuses_completed_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    real_publish = mod_materialization._publish_stream
    calls = 0

    def interrupted_publish(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_publish(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return result

    with mock.patch.object(mod_materialization, "_publish_stream", side_effect=interrupted_publish):
        with pytest.raises(RuntimeError, match="simulated interruption"):
            materialize_source(
                root=tmp_path,
                mod_name="Fixture",
                source=source,
                output_dir=output,
                context=load_game_profile("skyrim-se"),
                policy=policy_for(tmp_path),
                force=False,
                resume=True,
            )

    checkpoint = tmp_path / "work" / "shards" / "Fixture" / "materialization_checkpoint.jsonl"
    assert checkpoint.is_file()
    resumed = materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    assert resumed.reused_files == 1
    assert resumed.materialized_files == 1
    assert not checkpoint.exists()


def test_resume_rejects_corrupt_materialization_index(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    materialize_source(
        root=tmp_path,
        mod_name="Fixture",
        source=source,
        output_dir=output,
        context=load_game_profile("skyrim-se"),
        policy=policy_for(tmp_path),
        force=False,
        resume=True,
    )
    index = tmp_path / "work" / "shards" / "Fixture" / "index.json"
    index.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="use --force"):
        materialize_source(
            root=tmp_path,
            mod_name="Fixture",
            source=source,
            output_dir=output,
            context=load_game_profile("skyrim-se"),
            policy=policy_for(tmp_path),
            force=False,
            resume=True,
        )


def test_resume_rejects_corrupt_materialization_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "mod" / "Fixture"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    output = tmp_path / "work" / "extracted_mods" / "Fixture"
    checkpoint = tmp_path / "work" / "shards" / "Fixture" / "materialization_checkpoint.jsonl"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{"relative_path":"a.txt"}\n{broken\n', encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint.*use --force"):
        materialize_source(
            root=tmp_path,
            mod_name="Fixture",
            source=source,
            output_dir=output,
            context=load_game_profile("skyrim-se"),
            policy=policy_for(tmp_path),
            force=False,
            resume=True,
        )
