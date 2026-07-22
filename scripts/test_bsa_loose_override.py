from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_final_mod as subject  # noqa: E402
from game_context import load_game_context  # noqa: E402
from new_archive_audit_manifest import collect_file_rows, write_manifest  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bsa_workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path
    (root / ".skyrim-chs-workspace.json").write_text(
        json.dumps({"game_id": "skyrim-se"}), encoding="utf-8"
    )
    archive = root / "mod" / "Example.bsa"
    archive.parent.mkdir()
    archive.write_bytes(b"BSA-fixture")
    extracted_root = root / "work" / "archive_extracts" / "Example" / "Example"
    extracted = extracted_root / "Interface" / "translations" / "example.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text("source text", encoding="utf-8")
    manifest_dir = root / "out" / "Example" / "archive_audits" / "Example"
    report = root / "qa" / "Example.Example.archive_audit_manifest.md"
    write_manifest(
        root,
        "Example",
        archive,
        extracted_root,
        manifest_dir,
        report,
        collect_file_rows(root, extracted_root),
    )
    overlay = root / "translated" / "final_mod" / "Example" / "Interface" / "translations" / "example.txt"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("translated text", encoding="utf-8")
    return root, archive, extracted, overlay


def test_bsa_claim_produces_archive_bound_provenance(tmp_path: Path) -> None:
    root, archive, extracted, overlay = bsa_workspace(tmp_path)
    cache: dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]] = {}

    claims, sidecar_value = subject.load_bsa_loose_override_claims(root, "Example", cache)
    subject.require_bsa_claims_for_matching_overlays(root, "Example", claims, cache)

    final_mod = root / "out" / "Example" / "汉化产出" / "final_mod"
    destination = final_mod / "Interface" / "translations" / "example.txt"
    destination.parent.mkdir(parents=True)
    shutil.copy2(overlay, destination)
    record = {
        "Source": overlay.relative_to(root).as_posix(),
        "SourceSha256": sha256(overlay),
        "Destination": destination.relative_to(root).as_posix(),
        "Extension": ".txt",
        "ReplacesExistingFile": False,
        "BsaProvenance": claims[str(overlay.resolve()).lower()],
    }
    provenance = final_mod / "meta" / "provenance.jsonl"
    count = subject.write_provenance_jsonl(
        root,
        final_mod,
        provenance,
        [],
        [record],
        "Example",
        load_game_context(root),
    )

    rows = [json.loads(line) for line in provenance.read_text(encoding="utf-8").splitlines()]
    row = next(item for item in rows if item["file"].endswith("example.txt"))
    assert count == 2
    assert sidecar_value == ""
    assert row["transform"] == "bsa-loose-override"
    assert row["archive_path"] == archive.relative_to(root).as_posix()
    assert row["archive_sha256"] == sha256(archive)
    assert row["archive_entry_path"] == "Interface/translations/example.txt"
    assert row["archive_entry_sha256"] == sha256(extracted)
    assert row["archive_manifest"] == "out/Example/archive_audits/Example/manifest.json"


@pytest.mark.parametrize("tampered", ["archive", "extracted"])
def test_bsa_claim_rejects_tampered_archive_or_extracted_entry(
    tmp_path: Path,
    tampered: str,
) -> None:
    root, archive, extracted, _overlay = bsa_workspace(tmp_path)
    (archive if tampered == "archive" else extracted).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="BSA loose override"):
        subject.load_bsa_loose_override_claims(root, "Example", {})


def test_matching_bsa_overlay_binds_directly_from_generic_manifest(tmp_path: Path) -> None:
    root, _archive, _extracted, overlay = bsa_workspace(tmp_path)

    claims, _ = subject.load_bsa_loose_override_claims(root, "Example", {})

    assert str(overlay.resolve()).lower() in claims


def test_matching_bsa_overlay_with_incomplete_manifest_fails_closed(tmp_path: Path) -> None:
    root, _archive, _extracted, _overlay = bsa_workspace(tmp_path)
    manifest_path = root / "out" / "Example" / "archive_audits" / "Example" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("ArchiveSha256")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="BSA loose override"):
        subject.load_bsa_loose_override_claims(root, "Example", {})


def test_matching_bsa_overlay_with_malformed_manifest_fails_closed(tmp_path: Path) -> None:
    root, _archive, _extracted, _overlay = bsa_workspace(tmp_path)
    manifest_path = root / "out" / "Example" / "archive_audits" / "Example" / "manifest.json"
    manifest_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid archive audit manifest"):
        subject.load_bsa_loose_override_claims(root, "Example", {})


def test_archive_audit_manifest_discovery_rejects_hardlinks(tmp_path: Path) -> None:
    root, _archive, _extracted, _overlay = bsa_workspace(tmp_path)
    manifest_path = root / "out" / "Example" / "archive_audits" / "Example" / "manifest.json"
    external = root / "external-manifest.json"
    manifest_path.replace(external)
    os.link(external, manifest_path)

    with pytest.raises(ValueError, match="hardlink|multiple hardlinks"):
        subject.load_bsa_loose_override_claims(root, "Example", {})
