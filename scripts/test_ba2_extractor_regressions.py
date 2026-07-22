from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EXPECTED_PROTOCOL = "skyrim-mod-chs.ba2-extractor.v1"


def write_test_ba2(path: Path) -> None:
    entries = (
        ("Interface/translations/Example_en.txt", b"$HELLO\tHello"),
        ("MCM/Config/Example/settings.json", b'{"label":"Hello"}'),
    )
    names = b"".join(
        struct.pack("<H", len(name.encode("utf-8"))) + name.encode("utf-8")
        for name, _data in entries
    )
    names_offset = 24 + 36 * len(entries)
    payload_cursor = names_offset + len(names)
    records: list[bytes] = []
    payloads: list[bytes] = []
    for name, data in entries:
        extension = Path(name).suffix.lstrip(".").encode("ascii")[:4].ljust(4, b"\0")
        records.append(
            struct.pack(
                "<I4sIIQIII",
                0,
                extension,
                0,
                0,
                payload_cursor,
                0,
                len(data),
                0,
            )
        )
        payloads.append(data)
        payload_cursor += len(data)
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"GNRL", len(entries), names_offset)
        + b"".join(records)
        + names
        + b"".join(payloads)
    )


class Ba2ExtractorRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name)
        for relative in (
            "mod",
            "work/extracted_mods/TestMod",
            "work/archive_extracts",
            "out",
            "qa",
            "translated/final_mod/TestMod",
            "config",
            "tools",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        marker = {
            "schema_version": 2,
            "kind": "bethesda-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "game_id": "fallout4",
            "game_profile": "fallout4",
        }
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.archive = self.workspace / "mod" / "Example - Main.ba2"
        write_test_ba2(self.archive)
        self.adapter = self.workspace / "tools" / "fake_ba2_adapter.py"
        self.adapter.write_text(
            textwrap.dedent(
                """
                import argparse
                import os
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--archive-path", required=True)
                parser.add_argument("--output-dir", required=True)
                args = parser.parse_args()
                archive = Path(args.archive_path)
                output = Path(args.output_dir)
                output.mkdir(parents=True, exist_ok=True)
                mode = os.environ.get("FAKE_BA2_MODE", "normal")
                if mode == "fail":
                    (output / "partial.txt").write_text("partial", encoding="utf-8")
                    raise SystemExit(7)
                if mode == "modify-archive":
                    archive.write_bytes(archive.read_bytes() + b"-changed")
                if mode == "hardlink":
                    os.link(archive, output / "archive-hardlink.ba2")
                    raise SystemExit(0)
                if mode == "symlink":
                    os.symlink(archive, output / "archive-symlink.ba2")
                    raise SystemExit(0)
                if mode == "sibling-write":
                    (output.parent / "escaped-sibling.txt").write_text("escaped", encoding="utf-8")
                target = output / "Interface" / "translations" / "Example_en.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("$HELLO\\tHello", encoding="utf-8")
                settings = output / "MCM" / "Config" / "Example" / "settings.json"
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text('{"label":"Hello"}', encoding="utf-8")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.write_tools_config(adapter_path="tools/fake_ba2_adapter.py")

    def write_marker(self, game_id: str) -> None:
        marker = {
            "schema_version": 2,
            "kind": "bethesda-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "game_id": game_id,
            "game_profile": game_id,
        }
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "SKYRIM_CHS_WORKSPACE_ROOT": str(self.workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            **extra,
        }

    def write_tools_config(self, *, adapter_path: str, protocol: str = EXPECTED_PROTOCOL) -> None:
        config = {
            "DecoderTools": {
                "Ba2ExtractorPath": adapter_path,
                "Ba2ExtractorProtocol": protocol,
            }
        }
        (self.workspace / "config" / "tools.local.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_script(
        self,
        script: str,
        *args: str,
        mode: str = "normal",
        plugin_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.env(FAKE_BA2_MODE=mode)
        if plugin_root is not None:
            environment["SKYRIM_CHS_PLUGIN_ROOT"] = str(plugin_root)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @property
    def extracted_dir(self) -> Path:
        return self.workspace / "work" / "archive_extracts" / "TestMod" / "Example - Main"

    @property
    def manifest_path(self) -> Path:
        return self.workspace / "out" / "TestMod" / "archive_audits" / "Example - Main" / "manifest.json"

    def invoke_args(self) -> tuple[str, ...]:
        return (
            "--mod-name",
            "TestMod",
            "--archive-path",
            "mod/Example - Main.ba2",
            "--output-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--config-path",
            "config/tools.local.json",
        )

    def invoke(
        self,
        *extra: str,
        mode: str = "normal",
        plugin_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "invoke_ba2_extractor_safe.py",
            *self.invoke_args(),
            *extra,
            mode=mode,
            plugin_root=plugin_root,
        )

    def verify(self) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "verify_ba2_extraction.py",
            "--manifest-path",
            str(self.manifest_path.relative_to(self.workspace)),
        )

    def write_inventory_manifest(self, archive_path: Path) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import new_bsa_archive_manifest

        row = new_bsa_archive_manifest.ArchiveFileRow(
            RelativePath="Interface\\translations\\Example_en.txt",
            ProjectPath="",
            Extension=".txt",
            Size=12,
            Kind="interface-translation",
            Risk="translatable",
            RecommendedSkill="skills/text-resource-translation",
            Notes="inventory only",
        )
        new_bsa_archive_manifest.write_manifest(
            self.workspace,
            "TestMod",
            archive_path,
            self.manifest_path.parent,
            self.workspace / "qa" / "TestMod.Example - Main.archive_audit_manifest.md",
            [row],
        )

    def prepare_final_overlay(self) -> Path:
        overlay = self.workspace / "translated" / "final_mod" / "TestMod" / "Interface" / "translations" / "Example_en.txt"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("$HELLO\t你好", encoding="utf-8")
        dictionary = self.workspace / "translated" / "text_assets" / "TestMod" / "dictionary.jsonl"
        dictionary.parent.mkdir(parents=True, exist_ok=True)
        dictionary.write_text('{"Source":"Hello","Result":"你好"}\n', encoding="utf-8")
        return overlay

    def write_ba2_provenance_sidecar(self, *, entry_path: str, overlay_path: str) -> Path:
        return self.write_ba2_provenance_sidecars([(entry_path, overlay_path)])

    def write_ba2_provenance_sidecars(self, claims: list[tuple[str, str]]) -> Path:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        sidecar = self.workspace / "out" / "TestMod" / "archive_audits" / "ba2_loose_overrides.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for entry_path, overlay_path in claims:
            source_row = next(row for row in manifest["Files"] if row["RelativePath"] == entry_path)
            rows.append(
                {
                    "ManifestPath": str(self.manifest_path.relative_to(self.workspace)).replace("\\", "/"),
                    "ArchivePath": manifest["ArchivePath"],
                    "EntryPath": entry_path,
                    "OverlayPath": overlay_path,
                    "SourceSha256": source_row["Sha256"],
                }
            )
        sidecar.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return sidecar

    def test_safe_wrapper_generates_verified_manifest_from_materialized_files(self) -> None:
        adapter_result_path = self.workspace / "qa" / "Example.ba2.adapter_result.json"
        result = self.run_script(
            "invoke_ba2_extractor_safe.py",
            *self.invoke_args(),
            "--adapter-result-path",
            "qa/Example.ba2.adapter_result.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.extracted_dir / "Interface" / "translations" / "Example_en.txt").is_file())
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "skyrim-mod-chs.ba2-extraction-manifest")
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["game_id"], "fallout4")
        self.assertEqual(manifest["ModName"], "TestMod")
        self.assertEqual(manifest["FilesScanned"], 2)
        self.assertGreater(manifest["TotalBytes"], 0)
        self.assertFalse(manifest["allow_repack"])
        self.assertTrue(manifest["Safety"]["SourceArchiveUnchanged"])
        self.assertTrue(manifest["Safety"]["NoPathTraversal"])
        self.assertTrue(manifest["Safety"]["NoLinks"])
        self.assertTrue(manifest["Safety"]["NoRepack"])
        self.assertEqual(len(manifest["ArchiveSha256"]), 64)
        files_path = self.manifest_path.with_name("files.jsonl")
        rows = [json.loads(line) for line in files_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row["Sha256"]) == 64 for row in rows))
        receipt = json.loads(self.manifest_path.with_name("extraction_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["version"], 2)
        self.assertTrue(receipt["PayloadCapturedBeforePublication"])
        self.assertEqual(receipt["PayloadSnapshot"]["EntryCount"], 2)
        self.assertEqual(len(receipt["PayloadSnapshot"]["RootSha256"]), 64)
        self.assertEqual(len(receipt["BindingSha256"]), 64)
        self.assertEqual(manifest["PayloadRootSha256"], receipt["PayloadSnapshot"]["RootSha256"])
        self.assertEqual(manifest["ReceiptBindingSha256"], receipt["BindingSha256"])

        adapter_result = json.loads(adapter_result_path.read_text(encoding="utf-8"))
        self.assertEqual(adapter_result["status"], "success")
        self.assertEqual(adapter_result["operation"], "extract")
        self.assertEqual(adapter_result["adapter_id"], "bethesda-ba2")
        self.assertIn(
            str(self.manifest_path.relative_to(self.workspace)).replace("\\", "/"),
            adapter_result["evidence_files"],
        )
        self.assertTrue(adapter_result["artifacts"])
        for artifact in adapter_result["artifacts"]:
            artifact_path = self.workspace / artifact["path"]
            self.assertTrue(artifact_path.is_file())
            self.assertEqual(
                artifact["sha256"],
                hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            )

        verified = self.verify()
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_receipt_binding_rejects_payload_snapshot_or_limit_tampering(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt_path = self.manifest_path.with_name("extraction_receipt.json")
        original = receipt_path.read_text(encoding="utf-8")
        mutations = (
            lambda payload: payload["PayloadSnapshot"].__setitem__("RootSha256", "0" * 64),
            lambda payload: payload["Limits"].__setitem__("MaxFiles", payload["Limits"]["MaxFiles"] + 1),
            lambda payload: payload["ArchiveBefore"].__setitem__("sha256", "0" * 64),
        )
        for mutate in mutations:
            payload = json.loads(original)
            mutate(payload)
            receipt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.assertNotEqual(self.verify().returncode, 0)
        receipt_path.write_text(original, encoding="utf-8")
        self.assertEqual(self.verify().returncode, 0)

    def test_rejects_external_input_wrong_extension_and_inexact_output_layout(self) -> None:
        outside = self.workspace.parent / f"{self.workspace.name}-outside.ba2"
        outside.write_bytes(b"outside")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        wrong_extension = self.workspace / "mod" / "Example.zip"
        wrong_extension.write_bytes(b"not-ba2")
        cases = [
            ("--archive-path", str(outside)),
            ("--archive-path", "mod/Example.zip"),
            ("--output-dir", "work/archive_extracts/WrongMod/Example - Main"),
            ("--output-dir", "work/archive_extracts/TestMod/WrongArchive"),
            ("--output-dir", "work/archive_extracts/TestMod"),
            ("--mod-name", "../TestMod"),
        ]
        base = list(self.invoke_args())
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                args = base.copy()
                index = args.index(flag)
                args[index + 1] = value
                result = self.run_script("invoke_ba2_extractor_safe.py", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.extracted_dir.exists())

    def test_adapter_failures_preserve_previous_state_and_success_replaces_it(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        stale_manifest = self.manifest_path
        stale_manifest.write_text('{"stale":true}\n', encoding="utf-8")
        failed = self.invoke(mode="fail")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.extracted_dir.exists())
        self.assertEqual(stale_manifest.read_text(encoding="utf-8"), '{"stale":true}\n')
        self.assertEqual(list((self.workspace / "work" / "archive_extracts" / "TestMod").glob(".*.ba2-stage-*")), [])
        self.assertEqual(list(stale_manifest.parent.parent.glob(".*.ba2-evidence-*")), [])

        modified = self.invoke(mode="modify-archive")
        self.assertNotEqual(modified.returncode, 0)
        self.assertFalse(self.extracted_dir.exists())
        self.assertIn("source BA2 changed", modified.stderr)
        self.assertEqual(stale_manifest.read_text(encoding="utf-8"), '{"stale":true}\n')

        write_test_ba2(self.archive)
        self.extracted_dir.mkdir(parents=True)
        stale = self.extracted_dir / "stale.txt"
        stale.write_text("preserve", encoding="utf-8")
        replaced = self.invoke()
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(self.verify().returncode, 0)

    def test_standard_result_records_blocked_and_adapter_error_outcomes(self) -> None:
        result_path = self.workspace / "qa/Example.ba2.adapter_result.json"
        result_path.write_text('{"status":"success"}\n', encoding="utf-8")
        failed = self.invoke(
            "--adapter-result-path",
            "qa/Example.ba2.adapter_result.json",
            mode="fail",
        )
        self.assertEqual(failed.returncode, 1, failed.stderr)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "adapter_failed")
        self.assertFalse(self.extracted_dir.exists())

        self.write_marker("skyrim-se")
        blocked = self.invoke(
            "--adapter-result-path",
            "qa/Example.ba2.adapter_result.json",
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error_code"], "capability_unsupported")
        self.assertFalse(self.extracted_dir.exists())

    def test_failures_preserve_existing_verified_extraction_evidence(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt_path = self.manifest_path.with_name("extraction_receipt.json")
        expected_manifest = self.manifest_path.read_bytes()
        expected_receipt = receipt_path.read_bytes()
        expected_payload = (self.extracted_dir / "Interface" / "translations" / "Example_en.txt").read_bytes()

        original_archive = self.archive.read_bytes()
        self.archive.write_bytes(b"invalid-ba2")
        invalid_archive = self.invoke()
        self.assertNotEqual(invalid_archive.returncode, 0)
        self.assertEqual(self.manifest_path.read_bytes(), expected_manifest)
        self.assertEqual(receipt_path.read_bytes(), expected_receipt)
        self.assertEqual(
            (self.extracted_dir / "Interface" / "translations" / "Example_en.txt").read_bytes(),
            expected_payload,
        )
        self.archive.write_bytes(original_archive)

        self.write_tools_config(adapter_path="tools/fake_ba2_adapter.py")
        adapter_failure = self.invoke(mode="fail")
        self.assertNotEqual(adapter_failure.returncode, 0)
        self.assertEqual(self.manifest_path.read_bytes(), expected_manifest)
        self.assertEqual(receipt_path.read_bytes(), expected_receipt)
        self.assertEqual(
            (self.extracted_dir / "Interface" / "translations" / "Example_en.txt").read_bytes(),
            expected_payload,
        )

        self.write_tools_config(adapter_path="tools/fake_ba2_adapter.py", protocol="wrong-protocol")
        wrong_protocol = self.invoke()
        self.assertNotEqual(wrong_protocol.returncode, 0)
        self.assertEqual(self.manifest_path.read_bytes(), expected_manifest)
        self.assertEqual(receipt_path.read_bytes(), expected_receipt)

        self.write_tools_config(adapter_path="tools/missing_ba2_adapter.py")
        missing_adapter = self.invoke()
        self.assertNotEqual(missing_adapter.returncode, 0)
        self.assertEqual(self.manifest_path.read_bytes(), expected_manifest)
        self.assertEqual(receipt_path.read_bytes(), expected_receipt)

    def test_directory_publication_rolls_back_when_second_replace_fails(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import invoke_ba2_extractor_safe

        payload_target = self.workspace / "work" / "payload-target"
        evidence_target = self.workspace / "out" / "evidence-target"
        payload_staging = self.workspace / "work" / "payload-staging"
        evidence_staging = self.workspace / "out" / "evidence-staging"
        for directory, value in (
            (payload_target, "old-payload"),
            (evidence_target, "old-evidence"),
            (payload_staging, "new-payload"),
            (evidence_staging, "new-evidence"),
        ):
            directory.mkdir(parents=True)
            (directory / "value.txt").write_text(value, encoding="utf-8")

        real_replace = os.replace

        def fail_evidence_publish(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            if Path(source) == evidence_staging and Path(target) == evidence_target:
                raise OSError("simulated evidence publication failure")
            real_replace(source, target)

        with mock.patch.object(invoke_ba2_extractor_safe.os, "replace", side_effect=fail_evidence_publish):
            with self.assertRaisesRegex(OSError, "simulated evidence publication failure"):
                invoke_ba2_extractor_safe.publish_directories(
                    payload_staging,
                    payload_target,
                    evidence_staging,
                    evidence_target,
                )

        self.assertEqual((payload_target / "value.txt").read_text(encoding="utf-8"), "old-payload")
        self.assertEqual((evidence_target / "value.txt").read_text(encoding="utf-8"), "old-evidence")
        self.assertEqual(list(payload_target.parent.glob(".*.backup-*")), [])
        self.assertEqual(list(evidence_target.parent.glob(".*.backup-*")), [])

    def test_adapter_parent_sibling_write_is_detected_and_cleaned(self) -> None:
        result = self.invoke(mode="sibling-write")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staging", result.stderr.lower())
        self.assertFalse(self.extracted_dir.exists())
        staging_parent = self.workspace / "work" / "archive_extracts" / "TestMod"
        self.assertEqual(list(staging_parent.glob(".*.ba2-stage-*")), [])
        self.assertFalse((staging_parent / "escaped-sibling.txt").exists())

    def test_rejects_hardlinks_and_symlinks_without_publishing(self) -> None:
        hardlink = self.invoke(mode="hardlink")
        self.assertNotEqual(hardlink.returncode, 0)
        self.assertFalse(self.extracted_dir.exists())
        self.assertIn("hardlink", hardlink.stderr.lower())

        probe_target = self.workspace / "tools" / "symlink-probe-target"
        probe_link = self.workspace / "tools" / "symlink-probe-link"
        probe_target.write_text("probe", encoding="utf-8")
        try:
            os.symlink(probe_target, probe_link)
        except OSError:
            self.skipTest("Current Windows account cannot create symlinks")
        finally:
            probe_link.unlink(missing_ok=True)
        symlink = self.invoke(mode="symlink")
        self.assertNotEqual(symlink.returncode, 0)
        self.assertFalse(self.extracted_dir.exists())
        self.assertIn("link", symlink.stderr.lower())

    def test_rejects_symlink_or_reparse_archive_and_output_contract_paths(self) -> None:
        probe_target = self.workspace / "work" / "symlink-contract-probe"
        probe_link = self.workspace / "work" / "symlink-contract-link"
        probe_target.mkdir()
        try:
            os.symlink(probe_target, probe_link, target_is_directory=True)
        except OSError:
            self.skipTest("Current Windows account cannot create directory symlinks")
        finally:
            probe_link.unlink(missing_ok=True)
            probe_target.rmdir()

        redirect = self.workspace / "work" / "redirected-ba2-output"
        redirect.mkdir()
        self.extracted_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(redirect, self.extracted_dir, target_is_directory=True)
        output_result = self.invoke()
        if self.extracted_dir.is_symlink():
            self.extracted_dir.unlink()
        if redirect.exists():
            shutil.rmtree(redirect)
        self.assertNotEqual(output_result.returncode, 0)
        self.assertIn("link or reparse", output_result.stderr.lower())

        linked_archive = self.workspace / "mod" / "Linked.ba2"
        os.symlink(self.archive, linked_archive)
        args = list(self.invoke_args())
        args[args.index("--archive-path") + 1] = "mod/Linked.ba2"
        archive_result = self.run_script("invoke_ba2_extractor_safe.py", *args)
        self.assertNotEqual(archive_result.returncode, 0)
        self.assertIn("link or reparse", archive_result.stderr.lower())

    def test_rejects_archive_entry_path_forms_before_materialization(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        from new_ba2_archive_manifest import validate_archive_relative_path

        unsafe = (
            "",
            "../escape.txt",
            "folder/../escape.txt",
            "/absolute.txt",
            "\\absolute.txt",
            "\\\\server\\share\\file.txt",
            "C:\\absolute.txt",
            "C:relative.txt",
            "folder/NUL.txt",
            "folder/COM1",
            "nul\x00suffix.txt",
        )
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_archive_relative_path(value)

    def test_file_count_per_file_and_total_limits_fail_closed(self) -> None:
        cases = (
            ("--max-files", "1", "file count"),
            ("--max-file-bytes", "5", "per-file"),
            ("--max-total-bytes", "10", "total bytes"),
        )
        for flag, value, message in cases:
            with self.subTest(flag=flag):
                result = self.invoke(flag, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr.lower())
                self.assertFalse(self.extracted_dir.exists())

    def test_verifier_detects_archive_file_and_files_jsonl_drift(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        original_archive = self.archive.read_bytes()
        original_file = (self.extracted_dir / "Interface" / "translations" / "Example_en.txt").read_bytes()
        files_jsonl = self.manifest_path.with_name("files.jsonl")
        original_jsonl = files_jsonl.read_text(encoding="utf-8")

        self.archive.write_bytes(original_archive + b"changed")
        self.assertNotEqual(self.verify().returncode, 0)
        self.archive.write_bytes(original_archive)
        self.assertEqual(self.verify().returncode, 0)

        translated_file = self.extracted_dir / "Interface" / "translations" / "Example_en.txt"
        translated_file.write_bytes(original_file + b"changed")
        self.assertNotEqual(self.verify().returncode, 0)
        translated_file.write_bytes(original_file)
        self.assertEqual(self.verify().returncode, 0)

        files_jsonl.write_text("\n".join(original_jsonl.splitlines()[1:]) + "\n", encoding="utf-8")
        self.assertNotEqual(self.verify().returncode, 0)
        files_jsonl.write_text(original_jsonl + json.dumps({"RelativePath": "extra.txt"}) + "\n", encoding="utf-8")
        self.assertNotEqual(self.verify().returncode, 0)
        files_jsonl.write_text(original_jsonl, encoding="utf-8")
        self.assertEqual(self.verify().returncode, 0)

        (self.extracted_dir / "extra.txt").write_text("extra", encoding="utf-8")
        self.assertNotEqual(self.verify().returncode, 0)

    def test_verifier_rejects_manifest_game_extractor_project_path_and_safety_tampering(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        original_text = self.manifest_path.read_text(encoding="utf-8")
        original = json.loads(original_text)
        mutations = {
            "game-id": lambda data: data.__setitem__("game_id", "skyrim-se"),
            "extractor-hash": lambda data: data["ExtractorIdentity"].__setitem__("Sha256", "0" * 64),
            "extractor-size": lambda data: data["ExtractorIdentity"].__setitem__("Size", 1),
            "extractor-protocol": lambda data: data["ExtractorIdentity"].__setitem__("Protocol", "wrong"),
            "files-jsonl": lambda data: data.__setitem__("FilesJsonl", "qa/not-files.jsonl"),
            "project-path": lambda data: data["Files"][0].__setitem__("ProjectPath", "work/archive_extracts/TestMod/Wrong/file.txt"),
            "archive-mtime": lambda data: data.__setitem__("ArchiveMtimeNsBefore", 0),
            "audit-mode": lambda data: data.__setitem__("AuditMode", "hand-written"),
            "by-kind": lambda data: data.__setitem__("ByKind", {}),
            "published-atomically": lambda data: data["Safety"].__setitem__("PublishedAtomically", False),
            "project-local": lambda data: data["Safety"].__setitem__("ProjectLocalOnly", False),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = json.loads(original_text)
                mutate(payload)
                self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.assertNotEqual(self.verify().returncode, 0)
        self.manifest_path.write_text(original_text, encoding="utf-8")

        adapter_bytes = self.adapter.read_bytes()
        self.adapter.write_bytes(adapter_bytes + b"# tampered\n")
        self.assertNotEqual(self.verify().returncode, 0)
        self.adapter.write_bytes(adapter_bytes)
        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(original["game_id"], "fallout4")

    def test_standalone_manifest_cannot_raise_wrapper_extraction_limits(self) -> None:
        created = self.invoke(
            "--max-files",
            "3",
            "--max-file-bytes",
            "100",
            "--max-total-bytes",
            "100",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt = self.manifest_path.with_name("extraction_receipt.json")
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt_payload["Limits"],
            {"MaxFiles": 3, "MaxFileBytes": 100, "MaxTotalBytes": 100},
        )
        raised = self.run_script(
            "new_ba2_archive_manifest.py",
            "--mod-name",
            "TestMod",
            "--archive-path",
            "mod/Example - Main.ba2",
            "--extracted-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--extractor-path",
            "tools/fake_ba2_adapter.py",
            "--receipt-path",
            str(receipt.relative_to(self.workspace)),
            "--max-files",
            "4",
            "--max-file-bytes",
            "100",
            "--max-total-bytes",
            "100",
        )
        self.assertNotEqual(raised.returncode, 0)
        self.assertIn("cannot exceed receipt", raised.stderr.lower())

    def test_standalone_manifest_rejects_payload_added_removed_or_modified_after_receipt(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt = self.manifest_path.with_name("extraction_receipt.json")
        original = {
            path.relative_to(self.extracted_dir).as_posix(): path.read_bytes()
            for path in self.extracted_dir.rglob("*")
            if path.is_file()
        }

        def refresh_manifest() -> subprocess.CompletedProcess[str]:
            return self.run_script(
                "new_ba2_archive_manifest.py",
                "--mod-name",
                "TestMod",
                "--archive-path",
                "mod/Example - Main.ba2",
                "--extracted-dir",
                "work/archive_extracts/TestMod/Example - Main",
                "--extractor-path",
                "tools/fake_ba2_adapter.py",
                "--receipt-path",
                str(receipt.relative_to(self.workspace)),
            )

        modified_relative = next(iter(original))
        modified = self.extracted_dir / modified_relative
        modified.write_bytes(modified.read_bytes() + b"tampered")
        self.assertNotEqual(refresh_manifest().returncode, 0)
        modified.write_bytes(original[modified_relative])

        removed_relative = next(iter(original))
        removed = self.extracted_dir / removed_relative
        removed.unlink()
        self.assertNotEqual(refresh_manifest().returncode, 0)
        removed.parent.mkdir(parents=True, exist_ok=True)
        removed.write_bytes(original[removed_relative])

        added = self.extracted_dir / "Interface" / "translations" / "Added_en.txt"
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text("added", encoding="utf-8")
        self.assertNotEqual(refresh_manifest().returncode, 0)
        added.unlink()

        refreshed = refresh_manifest()
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertEqual(self.verify().returncode, 0)

    def test_standalone_manifest_requires_receipt_and_exact_archive_output_contract(self) -> None:
        self.extracted_dir.mkdir(parents=True)
        (self.extracted_dir / "file.txt").write_text("payload", encoding="utf-8")
        without_receipt = self.run_script(
            "new_ba2_archive_manifest.py",
            "--mod-name",
            "TestMod",
            "--archive-path",
            "mod/Example - Main.ba2",
            "--extracted-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--extractor-path",
            "tools/fake_ba2_adapter.py",
        )
        self.assertNotEqual(without_receipt.returncode, 0)
        self.assertIn("receipt", without_receipt.stderr.lower())
        self.assertFalse(self.manifest_path.exists())

        shutil.rmtree(self.extracted_dir)
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt = self.manifest_path.with_name("extraction_receipt.json")
        self.assertTrue(receipt.is_file())
        wrong_output = self.run_script(
            "new_ba2_archive_manifest.py",
            "--mod-name",
            "TestMod",
            "--archive-path",
            "mod/Example - Main.ba2",
            "--extracted-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--extractor-path",
            "tools/fake_ba2_adapter.py",
            "--receipt-path",
            str(receipt.relative_to(self.workspace)),
            "--output-dir",
            "out/TestMod/archive_audits/WrongArchive",
        )
        self.assertNotEqual(wrong_output.returncode, 0)
        self.assertIn("outputdir", wrong_output.stderr.lower())

        bad_archive = self.workspace / "qa" / "OutsideInput.ba2"
        bad_archive.write_bytes(self.archive.read_bytes())
        wrong_archive = self.run_script(
            "new_ba2_archive_manifest.py",
            "--mod-name",
            "TestMod",
            "--archive-path",
            "qa/OutsideInput.ba2",
            "--extracted-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--extractor-path",
            "tools/fake_ba2_adapter.py",
            "--receipt-path",
            str(receipt.relative_to(self.workspace)),
        )
        self.assertNotEqual(wrong_archive.returncode, 0)
        self.assertIn("mod/ or work/extracted_mods", wrong_archive.stderr)

        refreshed = self.run_script(
            "new_ba2_archive_manifest.py",
            "--mod-name",
            "TestMod",
            "--archive-path",
            "mod/Example - Main.ba2",
            "--extracted-dir",
            "work/archive_extracts/TestMod/Example - Main",
            "--extractor-path",
            "tools/fake_ba2_adapter.py",
            "--receipt-path",
            str(receipt.relative_to(self.workspace)),
        )
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertEqual(self.verify().returncode, 0)

    def test_external_adapter_is_not_treated_as_controlled(self) -> None:
        external_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, external_dir, True)
        external_adapter = external_dir / "fake_ba2_adapter.py"
        shutil.copy2(self.adapter, external_adapter)
        self.write_tools_config(adapter_path=str(external_adapter))
        isolated_plugin_root = self.workspace / "isolated-plugin-source"
        shutil.copytree(ROOT / "config", isolated_plugin_root / "config")
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import route_translation_task

        old_workspace = os.environ.get("SKYRIM_CHS_WORKSPACE_ROOT")
        old_plugin = os.environ.get("SKYRIM_CHS_PLUGIN_ROOT")
        os.environ.update(
            self.env(SKYRIM_CHS_PLUGIN_ROOT=str(isolated_plugin_root))
        )
        self.addCleanup(
            lambda: os.environ.__setitem__("SKYRIM_CHS_WORKSPACE_ROOT", old_workspace)
            if old_workspace is not None
            else os.environ.pop("SKYRIM_CHS_WORKSPACE_ROOT", None)
        )
        self.addCleanup(
            lambda: os.environ.__setitem__("SKYRIM_CHS_PLUGIN_ROOT", old_plugin)
            if old_plugin is not None
            else os.environ.pop("SKYRIM_CHS_PLUGIN_ROOT", None)
        )
        route = route_translation_task.route_for(self.workspace, self.archive)
        self.assertEqual(route.status, "ready")
        self.assertNotIn("invoke_ba2_extractor_safe.py", route.auxiliary_tool)
        invoked = self.invoke(plugin_root=isolated_plugin_root)
        self.assertNotEqual(invoked.returncode, 0)
        self.assertIn("workspace or plugin", invoked.stderr.lower())

        detected = self.run_script(
            "detect_decoder_tools.py",
            "--config-path",
            "config/tools.local.json",
            "--report-output-path",
            "qa/decoder_tools_report.md",
            "--as-json",
            plugin_root=isolated_plugin_root,
        )
        payload = json.loads(detected.stdout)
        ba2 = next(tool for tool in payload["Tools"] if tool["Property"] == "Ba2ExtractorPath")
        self.assertNotEqual(ba2["Status"], "ready")

    def test_detector_requires_explicit_safe_adapter_protocol(self) -> None:
        self.write_tools_config(adapter_path="tools/fake_ba2_adapter.py", protocol="wrong-protocol")
        result = self.run_script(
            "detect_decoder_tools.py",
            "--config-path",
            "config/tools.local.json",
            "--report-output-path",
            "qa/decoder_tools_report.md",
            "--as-json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ba2 = next(tool for tool in payload["Tools"] if tool["Property"] == "Ba2ExtractorPath")
        self.assertEqual(ba2["Status"], "requires-safe-adapter-protocol")
        self.assertFalse(any(tool["Role"] == "BA2" and tool["Status"] == "ready" for tool in payload["Tools"]))
        report = (self.workspace / "qa" / "decoder_tools_report.md").read_text(encoding="utf-8")
        self.assertIn("safe wrapper/adapter protocol", report)

    def test_read_only_ba2_inventory_cannot_materialize_translatable_loose_override(self) -> None:
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / self.archive.name
        shutil.copy2(self.archive, workspace_archive)
        self.write_inventory_manifest(workspace_archive)
        final_mod = self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod"
        loose = final_mod / "Interface" / "translations" / "Example_en.txt"
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text("$HELLO\t你好", encoding="utf-8")
        result = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            "work/extracted_mods/TestMod",
            "--final-mod-dir",
            "out/TestMod/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["Archives"][0]["MaterializationReady"])
        self.assertIn("safe-ba2-extraction-evidence-required", payload["LooseOverrides"][0]["Issues"])
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["ArchiveSha256"], hashlib.sha256(workspace_archive.read_bytes()).hexdigest())
        self.assertEqual(manifest["ArchiveSize"], workspace_archive.stat().st_size)

        workspace_archive.write_bytes(b"BTDX-readonly-replaced-with-same-name")
        replaced = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(replaced.returncode, 0)
        replaced_payload = json.loads(replaced.stdout)
        self.assertIn("archive-sha256-mismatch", replaced_payload["Archives"][0]["EvidenceIssues"])

    def test_production_readonly_bsa_manifest_refreshes_and_binds_current_archive(self) -> None:
        self.write_marker("skyrim-se")
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / "Example - Main.bsa"
        workspace_archive.write_bytes(b"BSA-production-fixture")
        self.write_inventory_manifest(workspace_archive)
        final_mod = self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod"
        loose = final_mod / "Interface" / "translations" / "Example_en.txt"
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text("$HELLO\t你好", encoding="utf-8")

        passed = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)

        workspace_archive.write_bytes(b"BSA-replaced-with-same-name")
        stale = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(stale.returncode, 0)
        stale_payload = json.loads(stale.stdout)
        self.assertIn("archive-sha256-mismatch", stale_payload["Archives"][0]["EvidenceIssues"])

        self.write_inventory_manifest(workspace_archive)
        refreshed = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)

    def test_production_extraction_manifest_binds_archive_and_rejects_replacement(self) -> None:
        self.write_marker("skyrim-se")
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import new_archive_audit_manifest

        archive = self.workspace / "work" / "extracted_mods" / "TestMod" / "Legacy.bsa"
        archive.write_bytes(b"BSA-extraction-backed")
        extracted = self.workspace / "work" / "archive_extracts" / "TestMod" / "Legacy"
        payload_file = extracted / "Meshes" / "payload.bin"
        payload_file.parent.mkdir(parents=True, exist_ok=True)
        payload_file.write_bytes(b"payload")
        output_dir = self.workspace / "out" / "TestMod" / "archive_audits" / "Legacy"
        report_path = self.workspace / "qa" / "TestMod.Legacy.archive_audit_manifest.md"
        rows = new_archive_audit_manifest.collect_file_rows(self.workspace, extracted)
        new_archive_audit_manifest.write_manifest(
            self.workspace,
            "TestMod",
            archive,
            extracted,
            output_dir,
            report_path,
            rows,
        )
        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["ArchiveSha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(manifest["ArchiveSize"], archive.stat().st_size)
        (self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod").mkdir(parents=True)

        passed = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)

        archive.write_bytes(b"BSA-extraction-backed-replaced")
        stale = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(stale.returncode, 0)
        stale_payload = json.loads(stale.stdout)
        self.assertIn("archive-sha256-mismatch", stale_payload["Archives"][0]["EvidenceIssues"])

    def test_legacy_manifest_without_archive_fingerprint_is_stale(self) -> None:
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / self.archive.name
        shutil.copy2(self.archive, workspace_archive)
        self.write_inventory_manifest(workspace_archive)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest.pop("ArchiveSha256")
        manifest.pop("ArchiveSize")
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod").mkdir(parents=True)

        result = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        issues = payload["Archives"][0]["EvidenceIssues"]
        self.assertIn("archive-evidence-stale-missing-ArchiveSha256", issues)
        self.assertIn("archive-evidence-stale-missing-ArchiveSize", issues)

    def test_archive_fingerprint_format_and_size_type_are_strict(self) -> None:
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / self.archive.name
        shutil.copy2(self.archive, workspace_archive)
        self.write_inventory_manifest(workspace_archive)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["ArchiveSha256"] = "z" * 64
        manifest["ArchiveSize"] = True
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod").mkdir(parents=True)

        result = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(result.returncode, 0)
        issues = json.loads(result.stdout)["Archives"][0]["EvidenceIssues"]
        self.assertIn("archive-sha256-invalid", issues)
        self.assertIn("archive-size-invalid", issues)

    def test_archive_coverage_reverifies_safe_ba2_manifest(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / self.archive.name
        shutil.copy2(self.archive, workspace_archive)
        final_mod = self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod"
        for row in json.loads(self.manifest_path.read_text(encoding="utf-8"))["Files"]:
            destination = final_mod / Path(row["RelativePath"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("translated", encoding="utf-8")
        passed = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            "work/extracted_mods/TestMod",
            "--final-mod-dir",
            "out/TestMod/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        payload = json.loads(passed.stdout)
        self.assertTrue(payload["Archives"][0]["MaterializationReady"])

        (self.extracted_dir / "Interface" / "translations" / "Example_en.txt").write_text("drift", encoding="utf-8")
        failed = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            "work/extracted_mods/TestMod",
            "--final-mod-dir",
            "out/TestMod/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(failed.returncode, 0)
        failed_payload = json.loads(failed.stdout)
        self.assertFalse(failed_payload["Archives"][0]["MaterializationReady"])

    def test_archive_coverage_rejects_same_name_ba2_with_different_content(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        workspace_archive = self.workspace / "work" / "extracted_mods" / "TestMod" / self.archive.name
        workspace_archive.write_bytes(b"BTDX-different-content-with-same-name")
        (self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod").mkdir(parents=True)

        result = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            "work/extracted_mods/TestMod",
            "--final-mod-dir",
            "out/TestMod/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        archive = next(row for row in payload["Archives"] if row["Scope"] == "workspace")
        self.assertFalse(archive["EvidenceValid"])
        self.assertTrue(
            any(issue in archive["EvidenceIssues"] for issue in ("archive-sha256-mismatch", "archive-size-mismatch"))
        )

    def test_final_mod_ba2_sidecar_produces_archive_entry_provenance(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        overlay = self.prepare_final_overlay()
        overlay_hash = hashlib.sha256(overlay.read_bytes()).hexdigest()
        self.write_ba2_provenance_sidecar(
            entry_path="Interface/translations/Example_en.txt",
            overlay_path="translated/final_mod/TestMod/Interface/translations/Example_en.txt",
        )
        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            "TestMod",
            "--source-mod-dir",
            "mod",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        provenance_path = self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod" / "meta" / "provenance.jsonl"
        rows = [json.loads(line) for line in provenance_path.read_text(encoding="utf-8").splitlines() if line]
        row = next(item for item in rows if item["file"].lower().endswith("interface/translations/example_en.txt"))
        self.assertEqual(row["transform"], "ba2-loose-override")
        self.assertEqual(row["source"], "translated/final_mod/TestMod/Interface/translations/Example_en.txt")
        self.assertEqual(row["source_sha256"], overlay_hash)
        self.assertEqual(row["archive_path"], "mod/Example - Main.ba2")
        self.assertEqual(row["archive_entry_path"], "Interface/translations/Example_en.txt")
        self.assertEqual(len(row["archive_entry_sha256"]), 64)
        self.assertIn("out/TestMod/archive_audits/Example - Main/manifest.json", row["qa_evidence"])

    def test_final_mod_verifies_each_canonical_ba2_manifest_only_once(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        first = self.prepare_final_overlay()
        second = self.workspace / "translated" / "final_mod" / "TestMod" / "MCM" / "Config" / "Example" / "settings.json"
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_text('{"label":"你好"}', encoding="utf-8")
        self.write_ba2_provenance_sidecars(
            [
                (
                    "Interface/translations/Example_en.txt",
                    str(first.relative_to(self.workspace)).replace("\\", "/"),
                ),
                (
                    "MCM/Config/Example/settings.json",
                    str(second.relative_to(self.workspace)).replace("\\", "/"),
                ),
            ]
        )

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import build_final_mod

        cache = {}
        with mock.patch.object(
            build_final_mod,
            "verify_ba2_manifest",
            wraps=build_final_mod.verify_ba2_manifest,
        ) as verifier:
            claims, _ = build_final_mod.load_ba2_loose_override_claims(self.workspace, "TestMod", cache)
            build_final_mod.require_ba2_claims_for_matching_overlays(self.workspace, "TestMod", claims, cache)
        self.assertEqual(verifier.call_count, 1)

    def test_final_mod_rejects_matching_ba2_overlay_without_provenance_sidecar(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.prepare_final_overlay()
        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            "TestMod",
            "--source-mod-dir",
            "mod",
            "--force",
        )
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("BA2 loose override", built.stderr)

    def test_final_mod_rejects_ba2_sidecar_relative_path_drift(self) -> None:
        created = self.invoke()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.prepare_final_overlay()
        self.write_ba2_provenance_sidecar(
            entry_path="MCM/Config/Example/settings.json",
            overlay_path="translated/final_mod/TestMod/Interface/translations/Example_en.txt",
        )
        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            "TestMod",
            "--source-mod-dir",
            "mod",
            "--force",
        )
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("BA2 loose override", built.stderr)

    def test_ba2_skill_and_example_config_document_only_safe_loose_delivery(self) -> None:
        skill = ROOT / "skills" / "ba2-archive-audit" / "SKILL.md"
        metadata = ROOT / "skills" / "ba2-archive-audit" / "agents" / "openai.yaml"
        self.assertTrue(skill.is_file())
        self.assertTrue(metadata.is_file())
        text = skill.read_text(encoding="utf-8")
        self.assertIn(EXPECTED_PROTOCOL, text)
        self.assertIn("invoke_ba2_extractor_safe.py", text)
        self.assertIn("verify_ba2_extraction.py", text)
        self.assertIn("allow_repack=false", text)
        self.assertNotIn("BA2 repack success", text)
        config = json.loads((ROOT / "config" / "tools.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["DecoderTools"]["Ba2ExtractorProtocol"], EXPECTED_PROTOCOL)
        opencode = json.loads((ROOT / "agents" / "opencode" / "adapter.json").read_text(encoding="utf-8"))
        claude = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertIn(".opencode/skills/ba2-archive-audit/SKILL.md", opencode["generated_config_files"])
        self.assertIn("./skills/ba2-archive-audit", claude["plugins"][0]["skills"])

    def test_existing_skyrim_bsa_route_and_safe_wrapper_contract_remain_unchanged(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import route_translation_task

        marker = json.loads((self.workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8"))
        marker["game_id"] = "skyrim-se"
        marker["game_profile"] = "skyrim-se"
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        bsa = self.workspace / "mod" / "SkyrimArchive.bsa"
        bsa.write_bytes(b"BSA-fake")
        route = route_translation_task.route_for(self.workspace, bsa)
        self.assertEqual(route.skill, "skills/bsa-archive-audit")
        self.assertIn("invoke_bsa_file_extractor_safe.py", route.auxiliary_tool)
        self.assertNotIn("invoke_ba2_extractor_safe.py", route.auxiliary_tool)
        self.assertTrue((SCRIPTS / "invoke_bsa_file_extractor_safe.py").is_file())

    def test_skyrim_profile_rejects_direct_ba2_wrapper_invocation(self) -> None:
        marker_path = self.workspace / ".skyrim-chs-workspace.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["game_id"] = "skyrim-se"
        marker["game_profile"] = "skyrim-se"
        marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        archive_before = self.archive.read_bytes()
        manifest_sentinel = b"existing-manifest-must-survive"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_bytes(manifest_sentinel)
        adapter_called = self.workspace / "tools" / "adapter-called.txt"
        self.adapter.write_text(
            "from pathlib import Path\nPath(__file__).with_name('adapter-called.txt').write_text('called')\n",
            encoding="utf-8",
        )

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("materialization is disabled", (result.stdout + result.stderr).lower())
        self.assertEqual(self.archive.read_bytes(), archive_before)
        self.assertFalse(self.extracted_dir.exists())
        self.assertEqual(self.manifest_path.read_bytes(), manifest_sentinel)
        self.assertFalse(adapter_called.exists())

    def test_route_uses_dedicated_skill_and_allows_audit_only_without_configured_adapter(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import route_translation_task

        os.environ.update(self.env())
        self.addCleanup(os.environ.pop, "SKYRIM_CHS_WORKSPACE_ROOT", None)
        self.addCleanup(os.environ.pop, "SKYRIM_CHS_PLUGIN_ROOT", None)

        ready = route_translation_task.route_for(self.workspace, self.archive)
        self.assertEqual(ready.skill, "skills/ba2-archive-audit")
        self.assertEqual(ready.status, "ready")
        self.assertIn("invoke_ba2_extractor_safe.py", ready.auxiliary_tool)
        self.assertIn("new_ba2_archive_manifest.py", ready.auxiliary_tool)
        self.assertIn("verify_ba2_extraction.py", ready.auxiliary_tool)
        self.assertNotIn("future", (ready.notes + ready.auxiliary_tool).lower())

        self.write_tools_config(adapter_path="")
        audit_only = route_translation_task.route_for(self.workspace, self.archive)
        self.assertEqual(audit_only.skill, "skills/ba2-archive-audit")
        self.assertEqual(audit_only.status, "ready")
        self.assertEqual(audit_only.blocked_reason, "")
        self.assertIn("read-only", audit_only.auxiliary_tool.lower())
        self.assertNotIn("invoke_ba2_extractor_safe.py", audit_only.auxiliary_tool)

    def test_detector_and_router_reject_linked_controlled_adapter_consistently(self) -> None:
        linked_adapter = self.workspace / "tools" / "linked_ba2_adapter.py"
        try:
            os.symlink(self.adapter, linked_adapter)
        except OSError:
            self.skipTest("Current Windows account cannot create file symlinks")
        self.write_tools_config(adapter_path="tools/linked_ba2_adapter.py")

        detected = self.run_script(
            "detect_decoder_tools.py",
            "--config-path",
            "config/tools.local.json",
            "--report-output-path",
            "qa/decoder_tools_report.md",
            "--as-json",
        )
        self.assertEqual(detected.returncode, 0, detected.stderr)
        payload = json.loads(detected.stdout)
        ba2 = next(tool for tool in payload["Tools"] if tool["Property"] == "Ba2ExtractorPath")
        self.assertNotEqual(ba2["Status"], "ready")

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import route_translation_task

        with mock.patch.dict(os.environ, self.env(), clear=False):
            route = route_translation_task.route_for(self.workspace, self.archive)
        self.assertEqual(route.status, "ready")
        self.assertNotIn("invoke_ba2_extractor_safe.py", route.auxiliary_tool)

        final_mod = self.workspace / "out" / "TestMod" / "汉化产出" / "final_mod"
        final_mod.mkdir(parents=True, exist_ok=True)
        coverage = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name", "TestMod",
            "--workspace-path", "work/extracted_mods/TestMod",
            "--final-mod-dir", "out/TestMod/汉化产出/final_mod",
            "--config-path", "config/tools.local.json",
            "--as-json",
        )
        self.assertEqual(coverage.returncode, 0, coverage.stderr)
        self.assertFalse(json.loads(coverage.stdout)["Ba2ExtractorReady"])

    def test_workflow_policy_authorizes_all_ba2_leaf_and_stage_commands(self) -> None:
        policy = json.loads((ROOT / "config" / "workflow_policy.json").read_text(encoding="utf-8"))
        scripts = {
            "scripts/new_ba2_archive_manifest.py",
            "scripts/invoke_ba2_extractor_safe.py",
            "scripts/verify_ba2_extraction.py",
        }
        self.assertTrue(scripts.issubset(set(policy["allowed_leaf_scripts"])))
        self.assertTrue(scripts.issubset(set(policy["states"]["routed"]["allowed_scripts"])))
        self.assertIn("scripts/verify_ba2_extraction.py", policy["states"]["final_mod_built"]["allowed_scripts"])
        self.assertIn(
            "ba2_extraction_required_without_adapter",
            policy["states"]["routed"]["blocked_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
