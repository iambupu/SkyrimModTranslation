from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import used_capabilities as subject  # noqa: E402
from audit_translation_readiness import collect_outputs  # noqa: E402
from run_non_gui_qa_gates import collect_used_capability_gate_issues  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace(tmp_path: Path, game_id: str) -> tuple[Path, Path]:
    root = tmp_path
    (root / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": game_id}), encoding="utf-8"
    )
    final_mod = root / "out" / "Example" / "汉化产出" / "final_mod"
    (final_mod / "meta").mkdir(parents=True)
    (root / "qa").mkdir()
    return root, final_mod


def provenance_row(
    root: Path,
    final_mod: Path,
    *,
    relative_file: str,
    source: str,
    transform: str,
    game_id: str,
    status: str = "assembled",
) -> dict[str, object]:
    final_path = final_mod / Path(relative_file)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(f"final:{relative_file}".encode())
    source_path = root / Path(source)
    if not source.startswith("generated:"):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(final_path.read_bytes())
        source_hash = sha256(source_path)
    else:
        source_hash = ""
    return {
        "game_id": game_id,
        "file": f"final_mod/{relative_file}",
        "file_sha256": sha256(final_path),
        "source": source,
        "source_sha256": source_hash,
        "transform": transform,
        "tool": "fixture",
        "generated_by": "build_final_mod.py",
        "status": status,
        "qa_evidence": ["qa/final_mod_validation.md"],
    }


def write_provenance(final_mod: Path, rows: list[dict[str, object]]) -> None:
    path = final_mod / "meta" / "provenance.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_apply_receipt(
    root: Path,
    *,
    stem: str,
    adapter_id: str,
    artifact: str,
    game_id: str,
    level: str,
    plugin_style: bool = False,
    traits: dict[str, str] | None = None,
    light_context: bool = False,
) -> Path:
    evidence = root / "qa" / f"{stem}.md"
    level_key = "plugin_text_capability_level" if plugin_style else "capability_level"
    heading = "# Mutagen Plugin Text Tool Report" if plugin_style else "# Mutagen PEX String Tool Report"
    adapter_line = f"- plugin_adapter: {adapter_id}\n" if plugin_style else ""
    output_label = "Output plugin" if plugin_style else "Output PEX"
    traits = traits or {
        "localized": "false",
        "light_by_extension": "false",
        "light_by_header": "false",
        "contains_unsupported_light_formids": "false",
    }
    artifact_path = root / artifact
    binary_suffix = ".esp" if plugin_style else ".pex"
    source_input = root / "work" / "extracted_mods" / "Example" / f"source{binary_suffix}"
    source_input.parent.mkdir(parents=True, exist_ok=True)
    source_input.write_bytes(b"source-binary")
    translation_input = (
        root / "translated" / "plugin_exports" / "Example" / "translation.jsonl"
        if plugin_style
        else root / "work" / "normalized" / "Example" / "translation.jsonl"
    )
    translation_input.parent.mkdir(parents=True, exist_ok=True)
    translation_input.write_text('{"source":"x","target":"y"}\n', encoding="utf-8")

    context_path: Path | None = None
    if plugin_style and light_context:
        context_path = (
            root
            / "work"
            / "plugin_context"
            / "Example"
            / "source.esp.resolved-master-styles.json"
        )
        context_path.parent.mkdir(parents=True, exist_ok=True)
        source_relative = source_input.relative_to(root).as_posix()
        context_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "game_id": game_id,
                    "plugin": source_input.name,
                    "input_path": source_relative,
                    "input_sha256": sha256(source_input),
                    "current_style": "light",
                    "current_evidence_source": "fixture:small-header",
                    "current_inspected_path": source_relative,
                    "current_inspected_sha256": sha256(source_input),
                    "masters": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    trait_lines = ""
    if plugin_style:
        trait_lines = "".join(f"- {key}: {value}\n" for key, value in traits.items())
        if context_path is None:
            trait_lines += "- Master-style context: <none>\n"
            trait_lines += "- Master-style context SHA256: <none>\n"
        else:
            trait_lines += (
                "- Master-style context: "
                f"{context_path.relative_to(root).as_posix()}\n"
            )
            trait_lines += f"- Master-style context SHA256: {sha256(context_path)}\n"
        trait_lines += "- Status: ready\n"
    evidence.write_text(
        f"{heading}\n\n- game_id: {game_id}\n{adapter_line}"
        f"- {level_key}: {level}\n{trait_lines}- {output_label}: {artifact}\n",
        encoding="utf-8",
    )
    artifact_rows = [
        {"path": artifact, "sha256": sha256(artifact_path)},
        {
            "path": evidence.relative_to(root).as_posix(),
            "sha256": sha256(evidence),
        },
    ]
    evidence_files = [evidence.relative_to(root).as_posix()]
    if context_path is not None:
        context_relative = context_path.relative_to(root).as_posix()
        artifact_rows.append({"path": context_relative, "sha256": sha256(context_path)})
        evidence_files.append(context_relative)
    receipt = root / "qa" / f"{stem}.adapter_result.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "success",
                "error_code": None,
                "operation": "apply",
                "adapter_id": adapter_id,
                "artifacts": artifact_rows,
                "evidence_files": evidence_files,
                "warnings": [],
                "blockers": [],
                "mod_name": "Example",
                "inputs": [
                    {
                        "path": source_input.relative_to(root).as_posix(),
                        "sha256": sha256(source_input),
                    },
                    {
                        "path": translation_input.relative_to(root).as_posix(),
                        "sha256": sha256(translation_input),
                    },
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return receipt


def test_loose_text_source_cannot_cross_mod_lane(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example.txt",
        source="translated/final_mod/OtherMod/Interface/example.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def capabilities(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = payload["capabilities"]
    assert isinstance(rows, list)
    return rows


def operations(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = payload["operations"]
    assert isinstance(rows, list)
    return rows


def test_operations_keep_same_capability_plugins_separate_by_resource_path(
    tmp_path: Path,
) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    rows = []
    for stem in ("Alpha", "Beta"):
        source = f"out/Example/tool_outputs/{stem}.esp"
        rows.append(
            provenance_row(
                root,
                final_mod,
                relative_file=f"{stem}.esp",
                source=source,
                transform="controlled-tool-output",
                game_id="fallout4",
            )
        )
        write_apply_receipt(
            root,
            stem=stem,
            adapter_id="mutagen-bethesda-plugin",
            artifact=source,
            game_id="fallout4",
            level="experimental_write",
            plugin_style=True,
        )
    write_provenance(final_mod, rows)

    payload = subject.collect_used_capabilities(root, "Example", final_mod)

    operation_rows = operations(payload)
    assert [row["resource_path"] for row in operation_rows] == ["Alpha.esp", "Beta.esp"]
    assert [row["capability"] for row in operation_rows] == ["plugin_text", "plugin_text"]
    assert "qa/Alpha.md" in operation_rows[0]["evidence"]
    assert "qa/Beta.md" not in operation_rows[0]["evidence"]
    assert "qa/Beta.md" in operation_rows[1]["evidence"]
    assert "qa/Alpha.md" not in operation_rows[1]["evidence"]

    capability_rows = capabilities(payload)
    assert len(capability_rows) == 1
    assert set(capability_rows[0]) == {
        "adapter_id",
        "evidence",
        "level",
        "name",
        "operation",
        "participates_in_final_delivery",
        "result",
        "strict_complete_allowed",
    }
    assert capability_rows[0]["name"] == "plugin_text"
    assert capability_rows[0]["evidence"] == sorted(
        [
            "out/Example/汉化产出/final_mod/meta/provenance.jsonl",
            "qa/Alpha.adapter_result.json",
            "qa/Alpha.md",
            "qa/Beta.adapter_result.json",
            "qa/Beta.md",
        ],
        key=str.casefold,
    )


def test_fallout4_loose_text_only_does_not_invent_experimental_usage(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/translations/example_english.txt",
        source="translated/final_mod/Example/Interface/translations/example_english.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    payload = subject.collect_used_capabilities(root, "Example", final_mod)

    assert capabilities(payload) == [
        {
            "adapter_id": "loose-text",
            "evidence": ["out/Example/汉化产出/final_mod/meta/provenance.jsonl"],
            "level": "stable",
            "name": "loose_text",
            "operation": "write",
            "participates_in_final_delivery": True,
            "result": "success",
            "strict_complete_allowed": True,
        }
    ]


def test_strict_gate_allows_fallout4_loose_text_delivery(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/translations/example_english.txt",
        source="translated/final_mod/Example/Interface/translations/example_english.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    issues, payload = collect_used_capability_gate_issues(
        root,
        "Example",
        final_mod,
        strict_complete=True,
    )

    assert issues == []
    assert [item["name"] for item in capabilities(payload)] == ["loose_text"]


def test_readiness_refreshes_and_reports_used_capability_evidence(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example.txt",
        source="translated/final_mod/Example/Interface/example.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    output = collect_outputs(root, ["Example"])[0]

    assert output.UsedCapabilitiesStatus == "passed"
    assert output.UsedCapabilitiesBlockingIssues == "0"
    assert (root / output.UsedCapabilitiesPath).is_file()

    (root / row["source"]).write_text("tampered", encoding="utf-8")
    output = collect_outputs(root, ["Example"])[0]

    assert output.UsedCapabilitiesStatus == "failed"
    assert output.UsedCapabilitiesBlockingIssues == "verification_failed"
    assert output.OverallStatus == "blocked_by_qa"


def test_readiness_normalizes_used_capability_output_filesystem_error(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example.txt",
        source="translated/final_mod/Example/Interface/example.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    (root / "qa" / "Example.used_capabilities.json").mkdir()

    output = collect_outputs(root, ["Example"])[0]

    assert output.UsedCapabilitiesStatus == "failed"
    assert output.UsedCapabilitiesBlockingIssues == "verification_failed"
    assert output.OverallStatus == "blocked_by_qa"


@pytest.mark.parametrize(
    ("relative_file", "source", "capability", "adapter", "plugin_style"),
    [
        (
            "Example.esp",
            "out/Example/tool_outputs/Example.esp",
            "plugin_text",
            "mutagen-bethesda-plugin",
            True,
        ),
        (
            "Scripts/Example.pex",
            "out/Example/tool_outputs/Scripts/Example.pex",
            "pex",
            "mutagen-pex",
            False,
        ),
    ],
)
def test_strict_gate_blocks_only_experimental_capabilities_used_in_delivery(
    tmp_path: Path,
    relative_file: str,
    source: str,
    capability: str,
    adapter: str,
    plugin_style: bool,
) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file=relative_file,
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    write_apply_receipt(
        root,
        stem=capability,
        adapter_id=adapter,
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=plugin_style,
    )

    issues, payload = collect_used_capability_gate_issues(
        root,
        "Example",
        final_mod,
        strict_complete=True,
    )

    assert [issue.Gate for issue in issues] == ["used-capability-experimental-restriction"]
    assert [item["name"] for item in capabilities(payload)] == [capability]


def test_strict_gate_fails_closed_when_used_capability_evidence_is_tampered(tmp_path: Path) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["artifacts"][0]["sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    issues, used_payload = collect_used_capability_gate_issues(
        root,
        "Example",
        final_mod,
        strict_complete=True,
    )

    assert used_payload == {}
    assert len(issues) == 1
    assert issues[0].Gate == "used-capability-verification-failed"


def test_original_binary_copies_do_not_count_as_write(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    rows = [
        provenance_row(
            root,
            final_mod,
            relative_file=name,
            source=f"mod/Example/{name}",
            transform="original-copy",
            game_id="fallout4",
        )
        for name in ("Example.esp", "Scripts/Example.pex")
    ]
    write_provenance(final_mod, rows)

    assert capabilities(subject.collect_used_capabilities(root, "Example", final_mod)) == []


def test_ba2_loose_override_records_verified_archive_read_and_loose_text_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    archive = root / "mod" / "Example.ba2"
    archive.parent.mkdir()
    archive.write_bytes(b"BA2-fixture")
    extracted = root / "work" / "archive_extracts" / "Example" / "Example" / "Interface" / "example.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text("source text", encoding="utf-8")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example.txt",
        source="translated/final_mod/Example/Interface/example.txt",
        transform="ba2-loose-override",
        game_id="fallout4",
    )
    manifest_path = root / "out" / "Example" / "archive_audits" / "Example" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "game_id": "fallout4",
        "ModName": "Example",
        "ArchivePath": "mod/Example.ba2",
        "ArchiveSha256": sha256(archive),
        "Files": [
            {
                "RelativePath": "Interface/example.txt",
                "ProjectPath": extracted.relative_to(root).as_posix(),
                "Sha256": sha256(extracted),
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    row.update(
        {
            "archive_path": "mod/Example.ba2",
            "archive_sha256": sha256(archive),
            "archive_entry_path": "Interface/example.txt",
            "archive_entry_sha256": sha256(extracted),
            "archive_manifest": manifest_path.relative_to(root).as_posix(),
        }
    )
    second_row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example-copy.txt",
        source="translated/final_mod/Example/Interface/example-copy.txt",
        transform="ba2-loose-override",
        game_id="fallout4",
    )
    second_row.update(
        {
            "archive_path": "mod/Example.ba2",
            "archive_sha256": sha256(archive),
            "archive_entry_path": "Interface/example.txt",
            "archive_entry_sha256": sha256(extracted),
            "archive_manifest": manifest_path.relative_to(root).as_posix(),
        }
    )
    write_provenance(final_mod, [row, second_row])
    receipt = root / "qa" / "Example.ba2_extract.adapter_result.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "success",
                "error_code": None,
                "operation": "extract",
                "adapter_id": "bethesda-ba2",
                "artifacts": [
                    {
                        "path": extracted.relative_to(root).as_posix(),
                        "sha256": sha256(extracted),
                    }
                ],
                "evidence_files": [manifest_path.relative_to(root).as_posix()],
                "warnings": [],
                "blockers": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    verify_calls: list[Path] = []

    def verify_once(_root: Path, path: Path) -> tuple[bool, list[str], dict[str, object]]:
        verify_calls.append(path)
        return True, [], manifest

    monkeypatch.setattr(subject, "verify_ba2_manifest", verify_once)

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [(item["name"], item["operation"]) for item in rows] == [
        ("archive.ba2", "read"),
        ("loose_text", "write"),
    ]
    assert rows[0]["adapter_id"] == "bethesda-ba2"
    assert rows[0]["level"] == "read_only"
    assert rows[0]["strict_complete_allowed"] is True
    assert rows[1]["strict_complete_allowed"] is True
    assert verify_calls == [manifest_path]


def write_generic_bsa_manifest(
    root: Path,
    archive: Path,
    extracted: Path,
) -> Path:
    manifest_path = root / "out" / "Example" / "archive_audits" / "Example" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path = "Interface/translations/example.txt"
    manifest = {
        "ModName": "Example",
        "ArchivePath": archive.relative_to(root).as_posix(),
        "ArchiveSha256": sha256(archive),
        "ArchiveSize": archive.stat().st_size,
        "ExtractedDir": (root / "work" / "archive_extracts" / "Example" / "Example").relative_to(root).as_posix(),
        "FilesScanned": 1,
        "ByKind": {"interface-translation": 1},
        "ByRisk": {"translatable": 1},
        "Files": [
            {
                "RelativePath": entry_path,
                "ProjectPath": extracted.relative_to(root).as_posix(),
                "Extension": ".txt",
                "Size": extracted.stat().st_size,
                "Kind": "interface-translation",
                "Risk": "translatable",
                "RecommendedSkill": "skills/text-resource-translation",
                "Notes": "fixture",
            }
        ],
        "Safety": {
            "ProjectLocalOnly": True,
            "ArchiveModified": False,
            "ExtractedContentModified": False,
            "RealGameDirectoriesAccessed": False,
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_bsa_extract_receipt(
    root: Path,
    archive: Path,
    extracted: Path,
    manifest_path: Path,
    *,
    stem: str = "Example.bsa_extract",
    input_path: Path | None = None,
    input_sha256: str | None = None,
    artifact_sha256: str | None = None,
) -> Path:
    bound_input = input_path or archive
    receipt = root / "qa" / f"{stem}.adapter_result.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "success",
                "error_code": None,
                "operation": "extract",
                "adapter_id": "bethesda-bsa",
                "artifacts": [
                    {
                        "path": extracted.relative_to(root).as_posix(),
                        "sha256": artifact_sha256 or sha256(extracted),
                    }
                ],
                "evidence_files": [manifest_path.relative_to(root).as_posix()],
                "warnings": [],
                "blockers": [],
                "mod_name": "Example",
                "inputs": [
                    {
                        "path": bound_input.relative_to(root).as_posix(),
                        "sha256": input_sha256 or sha256(bound_input),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return receipt


def bsa_used_capability_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, dict[str, object]]:
    root, final_mod = workspace(tmp_path, "skyrim-se")
    archive = root / "mod" / "Example.bsa"
    archive.parent.mkdir()
    archive.write_bytes(b"BSA-fixture")
    extracted = (
        root
        / "work"
        / "archive_extracts"
        / "Example"
        / "Example"
        / "Interface"
        / "translations"
        / "example.txt"
    )
    extracted.parent.mkdir(parents=True)
    extracted.write_text("source text", encoding="utf-8")
    manifest_path = write_generic_bsa_manifest(root, archive, extracted)
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/translations/example.txt",
        source="translated/final_mod/Example/Interface/translations/example.txt",
        transform="bsa-loose-override",
        game_id="skyrim-se",
    )
    row.update(
        {
            "archive_path": "mod/Example.bsa",
            "archive_sha256": sha256(archive),
            "archive_entry_path": "Interface/translations/example.txt",
            "archive_entry_sha256": sha256(extracted),
            "archive_manifest": manifest_path.relative_to(root).as_posix(),
        }
    )
    write_provenance(final_mod, [row])
    return root, final_mod, archive, extracted, manifest_path, row


def test_bsa_loose_override_records_verified_archive_read_with_unique_extract_receipt(
    tmp_path: Path,
) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    receipt = write_bsa_extract_receipt(root, archive, extracted, manifest_path)

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [(item["name"], item["operation"]) for item in rows] == [
        ("archive.bsa", "read"),
        ("loose_text", "write"),
    ]
    assert rows[0]["adapter_id"] == "bethesda-bsa"
    assert rows[0]["evidence"] == [
        manifest_path.relative_to(root).as_posix(),
        "out/Example/汉化产出/final_mod/meta/provenance.jsonl",
        receipt.relative_to(root).as_posix(),
    ]


def test_bsa_loose_override_missing_extract_receipt_fails_closed(tmp_path: Path) -> None:
    root, final_mod, _archive, _extracted, _manifest_path, _row = bsa_used_capability_fixture(tmp_path)

    with pytest.raises(subject.UsedCapabilityError, match="exactly one BSA AdapterResult"):
        subject.collect_used_capabilities(root, "Example", final_mod)


def test_bsa_loose_override_duplicate_extract_receipts_fail_closed(tmp_path: Path) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    write_bsa_extract_receipt(root, archive, extracted, manifest_path, stem="first")
    write_bsa_extract_receipt(root, archive, extracted, manifest_path, stem="second")

    with pytest.raises(subject.UsedCapabilityError, match="exactly one BSA AdapterResult"):
        subject.collect_used_capabilities(root, "Example", final_mod)


def test_bsa_loose_override_rejects_receipt_bound_to_wrong_archive_input(tmp_path: Path) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    other_archive = root / "mod" / "Other.bsa"
    other_archive.write_bytes(b"other-BSA")
    write_bsa_extract_receipt(
        root,
        archive,
        extracted,
        manifest_path,
        input_path=other_archive,
    )

    with pytest.raises(subject.UsedCapabilityError, match="source archive input"):
        subject.collect_used_capabilities(root, "Example", final_mod)


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "error"), ("adapter_id", "bethesda-ba2")],
)
def test_bsa_loose_override_requires_successful_bethesda_bsa_receipt(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    receipt = write_bsa_extract_receipt(root, archive, extracted, manifest_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload[field] = value
    if field == "status":
        payload["error_code"] = "adapter_failed"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError, match="successful extract"):
        subject.collect_used_capabilities(root, "Example", final_mod)


def test_bsa_loose_override_receipt_must_reference_verified_manifest(tmp_path: Path) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    receipt = write_bsa_extract_receipt(root, archive, extracted, manifest_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["evidence_files"] = []
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError, match="reference the verified manifest"):
        subject.collect_used_capabilities(root, "Example", final_mod)


@pytest.mark.parametrize("tampered_field", ["input", "artifact"])
def test_bsa_loose_override_rejects_receipt_hash_tampering(
    tmp_path: Path,
    tampered_field: str,
) -> None:
    root, final_mod, archive, extracted, manifest_path, _row = bsa_used_capability_fixture(tmp_path)
    kwargs = {
        "input_sha256": "0" * 64 if tampered_field == "input" else None,
        "artifact_sha256": "0" * 64 if tampered_field == "artifact" else None,
    }
    write_bsa_extract_receipt(root, archive, extracted, manifest_path, **kwargs)

    with pytest.raises(subject.UsedCapabilityError, match="hash mismatch"):
        subject.collect_used_capabilities(root, "Example", final_mod)


def test_bsa_loose_override_missing_archive_evidence_fails_closed(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "skyrim-se")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/translations/example.txt",
        source="translated/final_mod/Example/Interface/translations/example.txt",
        transform="bsa-loose-override",
        game_id="skyrim-se",
    )
    write_provenance(final_mod, [row])

    with pytest.raises(subject.UsedCapabilityError, match="missing archive evidence"):
        subject.collect_used_capabilities(root, "Example", final_mod)


@pytest.mark.parametrize(
    ("game_id", "relative_file", "source", "capability", "adapter", "level", "plugin_style"),
    [
        (
            "fallout4",
            "Example.esp",
            "out/Example/tool_outputs/Example.esp",
            "plugin_text",
            "mutagen-bethesda-plugin",
            "experimental_write",
            True,
        ),
        (
            "fallout4",
            "Scripts/Example.pex",
            "out/Example/tool_outputs/Scripts/Example.pex",
            "pex",
            "mutagen-pex",
            "experimental_write",
            False,
        ),
        (
            "skyrim-se",
            "Example.esp",
            "out/Example/tool_outputs/Example.esp",
            "plugin_text",
            "mutagen-bethesda-plugin",
            "stable",
            True,
        ),
    ],
)
def test_controlled_binary_delivery_requires_bound_apply_receipt(
    tmp_path: Path,
    game_id: str,
    relative_file: str,
    source: str,
    capability: str,
    adapter: str,
    level: str,
    plugin_style: bool,
) -> None:
    root, final_mod = workspace(tmp_path, game_id)
    row = provenance_row(
        root,
        final_mod,
        relative_file=relative_file,
        source=source,
        transform="controlled-tool-output",
        game_id=game_id,
    )
    write_provenance(final_mod, [row])
    write_apply_receipt(
        root,
        stem=capability,
        adapter_id=adapter,
        artifact=source,
        game_id=game_id,
        level=level,
        plugin_style=plugin_style,
    )

    payload = subject.collect_used_capabilities(root, "Example", final_mod)
    result = capabilities(payload)
    operation_rows = operations(payload)

    assert len(result) == 1
    assert result[0]["name"] == capability
    assert result[0]["level"] == level
    assert result[0]["result"] == "success"
    assert result[0]["strict_complete_allowed"] is (level == "stable")
    assert result[0]["participates_in_final_delivery"] is True
    assert result[0]["evidence"] == sorted(
        [
            f"qa/{capability}.adapter_result.json",
            f"qa/{capability}.md",
            "out/Example/汉化产出/final_mod/meta/provenance.jsonl",
        ],
        key=str.casefold,
    )
    assert len(operation_rows) == 1
    assert operation_rows[0]["capability"] == capability
    assert operation_rows[0]["effective_level"] == level
    assert operation_rows[0]["supported"] is True
    assert operation_rows[0]["error_code"] is None
    assert operation_rows[0]["reason"]
    assert operation_rows[0]["resource_category"]
    assert operation_rows[0]["resource_subtype"]
    assert "resource_container" in operation_rows[0]
    assert isinstance(operation_rows[0]["resource_traits"], list)


def test_plugin_report_traits_are_structured_and_reason_text_is_not_inferred(tmp_path: Path) -> None:
    from plugin_resource_evidence import read_plugin_report_traits

    report = tmp_path / "adapter.md"
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: unknown",
                "- light_by_header: true",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: <none>",
                "- Master-style context SHA256: <none>",
                "- Reason: localized light 0xFE words are not trait evidence",
            ]
        ),
        encoding="utf-8",
    )

    traits = read_plugin_report_traits(report)

    assert traits.localized is False
    assert traits.light_by_extension is None
    assert traits.light_by_header is True
    assert traits.contains_unsupported_light_formids is False
    assert traits.resource_traits() == frozenset({"light"})


def test_plugin_report_traits_require_all_four_fields(tmp_path: Path) -> None:
    from plugin_resource_evidence import read_plugin_report_traits

    report = tmp_path / "adapter.md"
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: false",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing plugin trait field"):
        read_plugin_report_traits(report)


def test_plugin_report_traits_reject_invalid_values(tmp_path: Path) -> None:
    from plugin_resource_evidence import read_plugin_report_traits

    report = tmp_path / "adapter.md"
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: maybe",
                "- contains_unsupported_light_formids: false",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid plugin trait value"):
        read_plugin_report_traits(report)


def test_fallout4_light_plugin_receipt_without_context_cannot_claim_write(
    tmp_path: Path,
) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
        traits={
            "localized": "false",
            "light_by_extension": "false",
            "light_by_header": "true",
            "contains_unsupported_light_formids": "false",
        },
    )

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"
    assert "master-style context" in str(error.value).lower()


def test_fallout4_light_plugin_receipt_with_context_claims_experimental_write(
    tmp_path: Path,
) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    receipt = write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
        light_context=True,
        traits={
            "localized": "false",
            "light_by_extension": "false",
            "light_by_header": "true",
            "contains_unsupported_light_formids": "false",
        },
    )

    payload = subject.collect_used_capabilities(root, "Example", final_mod)

    operation = operations(payload)[0]
    assert operation["effective_level"] == "experimental_write"
    assert operation["supported"] is True
    assert operation["strict_complete_allowed"] is False
    assert operation["resource_traits"] == ["light"]
    context = "work/plugin_context/Example/source.esp.resolved-master-styles.json"
    assert context in operation["evidence"]
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert context in receipt_payload["evidence_files"]
    assert context in {item["path"] for item in receipt_payload["artifacts"]}


def test_fallout4_unknown_plugin_trait_cannot_claim_write_capability(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
        traits={
            "localized": "false",
            "light_by_extension": "false",
            "light_by_header": "unknown",
            "contains_unsupported_light_formids": "false",
        },
    )

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "plugin_trait_unknown"


@pytest.mark.parametrize("drift", ["hash", "adapter", "game", "level"])
def test_controlled_binary_evidence_drift_fails_closed(tmp_path: Path, drift: str) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    receipt = write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
    )
    if drift == "hash":
        (root / source).write_bytes(b"tampered")
    elif drift == "adapter":
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["adapter_id"] = "mutagen-pex"
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    else:
        report = root / "qa" / "plugin_text.md"
        text = report.read_text(encoding="utf-8")
        if drift == "game":
            text = text.replace("game_id: fallout4", "game_id: skyrim-se")
        else:
            text = text.replace("experimental_write", "stable")
        report.write_text(text, encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code in {"adapter_failed", "verification_failed"}


def test_controlled_binary_source_receipt_and_final_hashes_must_match(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
    )
    final_file = final_mod / "Example.esp"
    final_file.write_bytes(b"different delivered binary")
    row["file_sha256"] = sha256(final_file)
    write_provenance(final_mod, [row])

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_mod_name_must_match_canonical_final_mod_path(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    write_provenance(final_mod, [])

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Other", final_mod)

    assert error.value.error_code == "profile_error"


def test_output_path_cannot_overwrite_another_mod_report(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    write_provenance(final_mod, [])
    other = root / "qa" / "Other.used_capabilities.json"
    other.write_text("sentinel", encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.write_used_capabilities(root, "Example", final_mod, other)

    assert error.value.error_code == "profile_error"
    assert other.read_text(encoding="utf-8") == "sentinel"


def test_binary_extension_cannot_claim_loose_text_translation(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Meshes/example.nif",
        source="translated/final_mod/Example/Meshes/example.nif",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_unknown_assembled_transform_fails_closed(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source="out/Example/tool_outputs/Example.esp",
        transform="controlled-tool-ouptut",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def valid_plugin_delivery(tmp_path: Path) -> tuple[Path, Path, dict[str, object], Path]:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/Example.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="Example.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    receipt = write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
    )
    return root, final_mod, row, receipt


def test_unrelated_malformed_receipt_does_not_block_current_mod(tmp_path: Path) -> None:
    root, final_mod, _row, _receipt = valid_plugin_delivery(tmp_path)
    (root / "qa" / "Other.adapter_result.json").write_text("{truncated", encoding="utf-8")

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [row["name"] for row in rows] == ["plugin_text"]


@pytest.mark.parametrize("mode", ["mod_name", "translation_input"])
def test_controlled_receipt_lineage_cannot_cross_mod_lane(
    tmp_path: Path,
    mode: str,
) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if mode == "mod_name":
        payload["mod_name"] = "OtherMod"
    else:
        other = root / "translated" / "plugin_exports" / "OtherMod" / "translation.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text('{"source":"x","target":"y"}\n', encoding="utf-8")
        payload["inputs"][1] = {
            "path": other.relative_to(root).as_posix(),
            "sha256": sha256(other),
        }
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_malformed_receipt_claiming_current_artifact_fails_closed(tmp_path: Path) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    receipt.unlink()
    (root / "qa" / "broken.adapter_result.json").write_text(
        '{"artifacts":[{"path":"out/Example/tool_outputs/Example.esp"}',
        encoding="utf-8",
    )

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_final_mod_file_cannot_be_its_own_loose_text_source(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    final_file = final_mod / "Interface" / "example.txt"
    final_file.parent.mkdir(parents=True)
    final_file.write_text("translated", encoding="utf-8")
    final_relative = final_file.relative_to(root).as_posix()
    write_provenance(
        final_mod,
        [
            {
                "game_id": "fallout4",
                "file": "final_mod/Interface/example.txt",
                "file_sha256": sha256(final_file),
                "source": final_relative,
                "source_sha256": sha256(final_file),
                "transform": "text-resource-translation",
                "tool": "fixture",
                "generated_by": "build_final_mod.py",
                "status": "assembled",
                "qa_evidence": [],
            }
        ],
    )

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_hash_bound_evidence_rejects_tampering_and_failure_report(tmp_path: Path) -> None:
    for mode in ("tampered", "failed"):
        case = tmp_path / mode
        case.mkdir()
        root, final_mod, _row, receipt = valid_plugin_delivery(case)
        report = root / "qa" / "plugin_text.md"
        if mode == "tampered":
            report.write_text(report.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        else:
            report.write_text(
                report.read_text(encoding="utf-8") + "- Status: failed\n",
                encoding="utf-8",
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            for artifact in payload["artifacts"]:
                if artifact["path"] == "qa/plugin_text.md":
                    artifact["sha256"] = sha256(report)
            receipt.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(subject.UsedCapabilityError) as error:
            subject.collect_used_capabilities(root, "Example", final_mod)

        assert error.value.error_code == "verification_failed"


@pytest.mark.parametrize("mode", ("missing", "duplicate", "illegal", "failed"))
def test_plugin_evidence_requires_exactly_one_successful_ready_status(
    tmp_path: Path,
    mode: str,
) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    report = root / "qa" / "plugin_text.md"
    text = report.read_text(encoding="utf-8")
    if mode == "missing":
        text = text.replace("- Status: ready\n", "")
    elif mode == "duplicate":
        text += "- Status: ready\n"
    elif mode == "illegal":
        text = text.replace("- Status: ready", "- Status: complete")
    else:
        text = text.replace("- Status: ready", "- Status: failed")
    report.write_text(text, encoding="utf-8")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        if artifact["path"] == "qa/plugin_text.md":
            artifact["sha256"] = sha256(report)
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_duplicate_provenance_and_receipt_artifact_paths_fail_closed(tmp_path: Path) -> None:
    for mode in ("provenance", "artifact"):
        case = tmp_path / mode
        case.mkdir()
        root, final_mod, row, receipt = valid_plugin_delivery(case)
        if mode == "provenance":
            write_provenance(final_mod, [row, dict(row)])
        else:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            payload["artifacts"].append(dict(payload["artifacts"][0]))
            receipt.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(subject.UsedCapabilityError) as error:
            subject.collect_used_capabilities(root, "Example", final_mod)

        assert error.value.error_code in {"adapter_failed", "verification_failed"}


def test_oversized_evidence_fails_with_controlled_error(tmp_path: Path) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    report = root / "qa" / "plugin_text.md"
    report.write_bytes(b"x" * (subject.MAX_EVIDENCE_BYTES + 1))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        if artifact["path"] == "qa/plugin_text.md":
            artifact["sha256"] = sha256(report)
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse behavior")
def test_final_mod_reparse_alias_is_rejected(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    write_provenance(final_mod, [])
    actual = root / "out" / "Example" / "actual-final"
    final_mod.rename(actual)
    try:
        os.symlink(actual, final_mod, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code in {"profile_error", "verification_failed"}


def test_receipt_candidate_sniff_decodes_backslashes_and_unicode(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    source = "out/Example/tool_outputs/测试.esp"
    row = provenance_row(
        root,
        final_mod,
        relative_file="测试.esp",
        source=source,
        transform="controlled-tool-output",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])
    receipt = write_apply_receipt(
        root,
        stem="plugin_text",
        adapter_id="mutagen-bethesda-plugin",
        artifact=source,
        game_id="fallout4",
        level="experimental_write",
        plugin_style=True,
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for input_item in payload["inputs"]:
        input_item["path"] = input_item["path"].replace("/", "\\")
    for artifact in payload["artifacts"]:
        artifact["path"] = artifact["path"].replace("/", "\\")
    payload["evidence_files"] = [value.replace("/", "\\") for value in payload["evidence_files"]]
    receipt.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [item["name"] for item in rows] == ["plugin_text"]


def test_malformed_escaped_duplicate_claim_is_not_hidden(tmp_path: Path) -> None:
    root, final_mod, _row, _receipt = valid_plugin_delivery(tmp_path)
    (root / "qa" / "broken.adapter_result.json").write_text(
        '{"artifacts":[{"path":"out\\\\Example\\\\tool_outputs\\\\Example.esp"}',
        encoding="utf-8",
    )

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "verification_failed"


def test_malformed_unrelated_warning_does_not_become_artifact_claim(tmp_path: Path) -> None:
    root, final_mod, _row, _receipt = valid_plugin_delivery(tmp_path)
    (root / "qa" / "Other.adapter_result.json").write_text(
        '{"warnings":["out/Example/tool_outputs/Example.esp"]',
        encoding="utf-8",
    )

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [item["name"] for item in rows] == ["plugin_text"]


def test_report_output_alias_uses_same_canonical_path_key(tmp_path: Path) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    report = root / "qa" / "plugin_text.md"
    report.write_text(
        report.read_text(encoding="utf-8").replace(
            "out/Example/tool_outputs/Example.esp",
            "out/Example/tool_outputs/segment/../Example.esp",
        ),
        encoding="utf-8",
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        if artifact["path"] == "qa/plugin_text.md":
            artifact["sha256"] = sha256(report)
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    rows = capabilities(subject.collect_used_capabilities(root, "Example", final_mod))

    assert [item["name"] for item in rows] == ["plugin_text"]


@pytest.mark.parametrize("alias", ["./", "segment/../"])
def test_receipt_alias_claims_are_normalized_before_duplicate_check(
    tmp_path: Path,
    alias: str,
) -> None:
    root, final_mod, _row, receipt = valid_plugin_delivery(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    duplicate = dict(payload["artifacts"][0])
    duplicate["path"] = f"out/Example/tool_outputs/{alias}Example.esp"
    duplicate["sha256"] = "0" * 64
    payload["artifacts"].append(duplicate)
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.collect_used_capabilities(root, "Example", final_mod)

    assert error.value.error_code == "adapter_failed"


def test_missing_provenance_removes_stale_success_output(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    output = root / "qa" / "Example.used_capabilities.json"
    output.write_text('{"stale": true}', encoding="utf-8")

    with pytest.raises(subject.UsedCapabilityError) as error:
        subject.write_used_capabilities(root, "Example", final_mod, output)

    assert error.value.error_code == "verification_failed"
    assert not output.exists()


def test_write_is_deterministic_and_atomic(tmp_path: Path) -> None:
    root, final_mod = workspace(tmp_path, "fallout4")
    row = provenance_row(
        root,
        final_mod,
        relative_file="Interface/example.txt",
        source="translated/final_mod/Example/Interface/example.txt",
        transform="text-resource-translation",
        game_id="fallout4",
    )
    write_provenance(final_mod, [row])

    output = subject.write_used_capabilities(root, "Example", final_mod)
    first = output.read_bytes()
    subject.write_used_capabilities(root, "Example", final_mod)

    assert output.read_bytes() == first
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))
