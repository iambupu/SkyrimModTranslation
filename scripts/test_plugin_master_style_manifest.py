from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugin_master_style_manifest import (
    create_cached_sha256_resolver,
    prepare_master_style_manifest,
    read_plugin_header,
    sha256_file,
)
from plugin_resource_evidence import (
    discover_regular_plugin_files,
    plugin_artifact_key,
)


def write_plugin(
    path: Path,
    *,
    masters: tuple[str, ...] = (),
    small: bool = False,
    extended_first_master: bool = False,
) -> None:
    data = bytearray()
    for index, master in enumerate(masters):
        payload = master.encode("utf-8") + b"\0"
        if index == 0 and extended_first_master:
            data.extend(b"XXXX" + (4).to_bytes(2, "little") + len(payload).to_bytes(4, "little"))
            data.extend(b"MAST" + (0).to_bytes(2, "little") + payload)
        else:
            data.extend(b"MAST" + len(payload).to_bytes(2, "little") + payload)
    flags = 0x00000200 if small else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"TES4"
        + len(data).to_bytes(4, "little")
        + flags.to_bytes(4, "little")
        + (b"\0" * 12)
        + data
    )


def workspace(
    tmp_path: Path,
    *,
    small: bool = True,
    master_name: str = "CustomMaster.esm",
) -> tuple[Path, Path]:
    root = tmp_path
    plugin = root / "work" / "extracted_mods" / "Example" / "Patch.esp"
    write_plugin(plugin, masters=(master_name,), small=small)
    return root, plugin


def test_preflight_generates_hash_bound_manifest_from_workspace_master(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path)
    master = root / "work" / "master_context" / "fallout4" / "CustomMaster.esm"
    write_plugin(master)

    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        required_masters=("CustomMaster.esm",),
    )

    assert manifest is not None
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["plugin"] == "Patch.esp"
    assert payload["masters"][0]["mod_key"] == "CustomMaster.esm"
    assert payload["masters"][0]["master_style"] == "full"
    assert payload["masters"][0]["inspected_path"] == "work/master_context/fallout4/CustomMaster.esm"


def test_preflight_blocks_before_translation_when_master_evidence_is_missing(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path)

    with pytest.raises(ValueError, match="master_style_unknown"):
        prepare_master_style_manifest(
            root=root,
            game_id="fallout4",
            mod_name="Example",
            plugin=plugin,
            relative_plugin=Path("Patch.esp"),
            required_masters=("CustomMaster.esm",),
        )


def test_preflight_ignores_unrelated_missing_master_without_target_request(
    tmp_path: Path,
) -> None:
    root, plugin = workspace(tmp_path)
    hashed: list[Path] = []

    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        sha256_resolver=lambda path: hashed.append(path) or "a" * 64,
    )

    assert manifest is None
    assert hashed == []


def test_target_preflight_trims_legacy_broad_manifest_without_hashing_unrelated_master(
    tmp_path: Path,
) -> None:
    root = tmp_path
    plugin = root / "work" / "extracted_mods" / "Example" / "Patch.esp"
    write_plugin(
        plugin,
        masters=("TargetMaster.esm", "UnrelatedMaster.esm"),
        small=True,
    )
    target = root / "work" / "master_context" / "fallout4" / "TargetMaster.esm"
    unrelated = (
        root / "work" / "master_context" / "fallout4" / "UnrelatedMaster.esm"
    )
    write_plugin(target)
    write_plugin(unrelated)
    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        required_masters=("TargetMaster.esm", "UnrelatedMaster.esm"),
    )
    assert manifest is not None
    unrelated.unlink()
    hashed: list[Path] = []

    def record_hash(path: Path) -> str:
        hashed.append(path)
        return sha256_file(path)

    reused = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        required_masters=("TargetMaster.esm",),
        sha256_resolver=record_hash,
    )

    assert reused == manifest
    assert hashed == [target.resolve()]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert [row["mod_key"] for row in payload["masters"]] == ["TargetMaster.esm"]


def test_ordinary_full_plugin_does_not_require_game_master_evidence(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path, small=False, master_name="Fallout4.esm")

    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
    )

    assert manifest is None


def test_light_plugin_does_not_require_known_full_game_master_file(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path, master_name="Fallout4.esm")
    hashed: list[Path] = []

    def record_hash(path: Path) -> str:
        hashed.append(path)
        return "a" * 64

    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        sha256_resolver=record_hash,
    )

    assert manifest is None
    assert hashed == []


def test_same_basename_plugins_receive_distinct_manifest_paths(tmp_path: Path) -> None:
    root = tmp_path
    master = root / "work" / "master_context" / "fallout4" / "CustomMaster.esm"
    write_plugin(master)
    manifests: list[Path] = []
    for relative in (Path("A/Patch.esp"), Path("B/Patch.esp")):
        plugin = root / "work" / "extracted_mods" / "Example" / relative
        write_plugin(plugin, masters=("CustomMaster.esm",), small=True)
        manifest = prepare_master_style_manifest(
            root=root,
            game_id="fallout4",
            mod_name="Example",
            plugin=plugin,
            relative_plugin=relative,
            required_masters=("CustomMaster.esm",),
        )
        assert manifest is not None
        assert manifest.name == f"{plugin_artifact_key('Example', relative)}.master-styles.json"
        manifests.append(manifest)

    assert manifests[0] != manifests[1]


def test_shared_hash_resolver_reuses_unchanged_master_across_plugins(tmp_path: Path) -> None:
    root = tmp_path
    master = root / "work" / "master_context" / "fallout4" / "CustomMaster.esm"
    write_plugin(master)
    calls: list[Path] = []

    def counting_resolver(path: Path) -> str:
        calls.append(path)
        return "a" * 64

    cached_resolver = create_cached_sha256_resolver(counting_resolver)
    for relative in (Path("A/Patch.esp"), Path("B/Patch.esp")):
        plugin = root / "work" / "extracted_mods" / "Example" / relative
        write_plugin(plugin, masters=("CustomMaster.esm",), small=True)
        prepare_master_style_manifest(
            root=root,
            game_id="fallout4",
            mod_name="Example",
            plugin=plugin,
            relative_plugin=relative,
            required_masters=("CustomMaster.esm",),
            sha256_resolver=cached_resolver,
        )

    assert calls == [master.resolve()]


def test_cached_hash_resolver_rehashes_changed_master(tmp_path: Path) -> None:
    master = tmp_path / "Master.esm"
    write_plugin(master)
    calls = 0

    def counting_resolver(path: Path) -> str:
        nonlocal calls
        calls += 1
        return str(calls) * 64

    cached_resolver = create_cached_sha256_resolver(counting_resolver)

    assert cached_resolver(master) == "1" * 64
    master.write_bytes(master.read_bytes() + b"changed")
    assert cached_resolver(master) == "2" * 64
    assert calls == 2


def test_cached_hash_resolver_rejects_master_changed_while_hashing(
    tmp_path: Path,
) -> None:
    master = tmp_path / "Master.esm"
    write_plugin(master)

    def changing_resolver(path: Path) -> str:
        path.write_bytes(path.read_bytes() + b"changed")
        return "a" * 64

    cached_resolver = create_cached_sha256_resolver(changing_resolver)

    with pytest.raises(ValueError, match="master_style_evidence_stale"):
        cached_resolver(master)


def test_preflight_reads_mast_after_xxxx_extended_subrecord(tmp_path: Path) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "Example" / "Extended.esp"
    write_plugin(plugin, masters=("Fallout4.esm",), extended_first_master=True)

    header = read_plugin_header(plugin)

    assert header.masters == ("Fallout4.esm",)


def test_generated_manifest_blocks_when_master_hash_becomes_stale(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path)
    master = root / "work" / "master_context" / "fallout4" / "CustomMaster.esm"
    write_plugin(master)
    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        required_masters=("CustomMaster.esm",),
    )
    assert manifest is not None
    master.write_bytes(master.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="master_style_evidence_stale"):
        prepare_master_style_manifest(
            root=root,
            game_id="fallout4",
            mod_name="Example",
            plugin=plugin,
            relative_plugin=Path("Patch.esp"),
            required_masters=("CustomMaster.esm",),
        )


def test_existing_manifest_invalid_utf8_uses_stable_conflict_code(tmp_path: Path) -> None:
    root, plugin = workspace(tmp_path)
    master = root / "work" / "master_context" / "fallout4" / "CustomMaster.esm"
    write_plugin(master)
    manifest = prepare_master_style_manifest(
        root=root,
        game_id="fallout4",
        mod_name="Example",
        plugin=plugin,
        relative_plugin=Path("Patch.esp"),
        required_masters=("CustomMaster.esm",),
    )
    assert manifest is not None
    manifest.write_bytes(b'{"schema_version":2,"game_id":"fallout4","bad":"\xff"}')

    with pytest.raises(ValueError, match="master_style_conflict"):
        prepare_master_style_manifest(
            root=root,
            game_id="fallout4",
            mod_name="Example",
            plugin=plugin,
            relative_plugin=Path("Patch.esp"),
            required_masters=("CustomMaster.esm",),
        )


def test_plugin_discovery_rejects_hardlinked_input(tmp_path: Path) -> None:
    workspace_root = tmp_path / "work" / "extracted_mods" / "Example"
    workspace_root.mkdir(parents=True)
    outside = tmp_path / "outside.esp"
    write_plugin(outside)
    os.link(outside, workspace_root / "Linked.esp")

    with pytest.raises(ValueError, match="multiple hardlinks"):
        discover_regular_plugin_files(
            workspace_root,
            {".esp", ".esm", ".esl"},
            label="Plugin test input",
        )
