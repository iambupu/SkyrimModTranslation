from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_final_mod  # noqa: E402
import audit_pex_delivery  # noqa: E402
import audit_translation_readiness  # noqa: E402
import extract_non_gui_candidates  # noqa: E402
import extract_mcm_text  # noqa: E402
import new_final_text_review_packet  # noqa: E402
import new_final_binary_review_packet  # noqa: E402
import new_ba2_archive_manifest  # noqa: E402
import plugin_resource_evidence  # noqa: E402
import project_paths  # noqa: E402
import proofread_translation  # noqa: E402
import run_non_gui_qa_gates  # noqa: E402
import run_non_gui_translation_workflow  # noqa: E402
import scan_placeholders  # noqa: E402
import translation_dictionary  # noqa: E402
import used_capabilities  # noqa: E402
import validate_chs_package  # noqa: E402
import validate_final_mod  # noqa: E402
from file_utils import (  # noqa: E402
    create_regular_directory_under,
    discover_regular_files,
    validate_regular_path_under,
)
from game_context import load_game_profile  # noqa: E402


class PathSafetyRegressionTests(unittest.TestCase):
    @staticmethod
    def _windows_path_variant(path: Path, function_name: str) -> Path:
        import ctypes

        function = getattr(ctypes.windll.kernel32, function_name)
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(str(path), buffer, len(buffer))
        if length == 0 or length >= len(buffer):
            raise OSError(f"{function_name} failed for {path}")
        return Path(buffer.value)

    def test_strict_relative_path_resolves_aliases_before_comparison(self) -> None:
        canonical_root = ROOT.resolve()
        canonical_value = canonical_root / "out" / "Example" / "result.txt"

        class AliasPath:
            def __init__(self, canonical: Path) -> None:
                self.canonical = canonical

            def resolve(self, *, strict: bool) -> Path:
                return self.canonical

            def relative_to(self, _other: object) -> Path:
                raise AssertionError("lexical aliases must not be compared directly")

        relative = project_paths.relative_posix_strict(
            AliasPath(canonical_root),  # type: ignore[arg-type]
            AliasPath(canonical_value),  # type: ignore[arg-type]
        )

        self.assertEqual("out/Example/result.txt", relative)

    @unittest.skipUnless(os.name == "nt", "Windows path comparison regression")
    def test_regular_path_validation_accepts_case_variant_root(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "work" / "source.txt"
            source.parent.mkdir()
            source.write_text("fixture\n", encoding="utf-8")

            case_variant_root = Path(str(root).swapcase())
            validated = validate_regular_path_under(
                source,
                case_variant_root,
                kind="file",
                label="fixture",
            )

            self.assertEqual(source.resolve(), validated)

    @unittest.skipUnless(os.name == "nt", "Windows path alias regression")
    def test_public_path_guards_accept_short_and_long_aliases(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            long_root = self._windows_path_variant(Path(temp_dir), "GetLongPathNameW")
            short_root = self._windows_path_variant(long_root, "GetShortPathNameW")
            if os.path.normcase(str(long_root)) == os.path.normcase(str(short_root)):
                self.skipTest("8.3 path aliases are not available on this volume")

            source = long_root / "work" / "source.txt"
            source.parent.mkdir()
            source.write_text("fixture\n", encoding="utf-8")

            validated = validate_regular_path_under(
                source,
                short_root,
                kind="file",
                label="fixture",
            )
            created = create_regular_directory_under(
                long_root / "out" / "new",
                short_root,
                label="fixture output",
            )
            resolved = new_ba2_archive_manifest.resolve_workspace_contract_path(
                short_root,
                source,
                must_exist=True,
            )
            evidence = plugin_resource_evidence.validate_regular_evidence_path_under(
                source,
                short_root,
                kind="file",
                label="fixture evidence",
            )
            used_capabilities._validate_no_reparse_chain(source, short_root)

            self.assertEqual(source.resolve(), validated)
            self.assertTrue(created.is_dir())
            self.assertEqual(source.resolve(), resolved)
            self.assertEqual(source.resolve(), evidence)

    @unittest.skipIf(os.name == "nt", "POSIX path comparison regression")
    def test_project_containment_respects_platform_case_semantics(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertFalse(
                project_paths.is_under(
                    root / "case-sensitive" / "file.txt",
                    root / "CASE-SENSITIVE",
                )
            )

    def test_special_path_segments_do_not_survive(self) -> None:
        for value in ("", ".", "..", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    project_paths.safe_file_name(value)

    def test_windows_reserved_names_are_prefixed(self) -> None:
        for value in ("CON", "con.txt", "LPT1", "COM9.log", "NUL"):
            with self.subTest(value=value):
                result = project_paths.safe_file_name(value)
                basename = result.split(".", 1)[0].upper()
                self.assertNotIn(basename, project_paths.WINDOWS_RESERVED_FILE_NAMES)

    def test_trailing_spaces_and_dots_are_removed(self) -> None:
        result = project_paths.safe_file_name("example.  ")
        self.assertFalse(result.endswith((".", " ")))

    def test_all_callers_share_the_central_implementation(self) -> None:
        self.assertIs(build_final_mod.safe_file_name, project_paths.safe_file_name)
        definitions = []
        for path in SCRIPTS.glob("*.py"):
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if any(line.startswith("def safe_file_name(") for line in lines):
                definitions.append(path.name)
        self.assertEqual(definitions, ["project_paths.py"])

    def test_non_gui_extraction_rejects_hardlinked_workspace_input(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".skyrim-chs-workspace.json").write_text(
                json.dumps({"schema_version": 1, "game_id": "skyrim-se"}),
                encoding="utf-8",
            )
            outside = root / "outside.json"
            outside.write_text('{"label":"Secret Visible Text"}', encoding="utf-8")
            workspace = root / "work" / "extracted_mods" / "Fixture" / "MCM"
            workspace.mkdir(parents=True)
            linked = workspace / "config.json"
            os.link(outside, linked)

            with mock.patch.object(extract_non_gui_candidates, "project_root", return_value=root):
                with mock.patch.object(
                    sys,
                    "argv",
                    ["extract_non_gui_candidates.py", "--mod-name", "Fixture"],
                ):
                    with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                        extract_non_gui_candidates.main()

    def test_quality_readers_reject_hardlinked_workspace_inputs(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text('{"label":"Visible Text"}', encoding="utf-8")
            workspace = root / "work" / "extracted_mods" / "Fixture" / "MCM"
            workspace.mkdir(parents=True)
            linked = workspace / "config.json"
            os.link(outside, linked)

            readers = (
                lambda: extract_mcm_text.collect_input_files(workspace),
                lambda: new_final_text_review_packet.collect_supported_files(workspace),
                lambda: new_final_binary_review_packet.binary_fingerprints(workspace),
                lambda: proofread_translation.load_allowed_words(root),
            )
            for reader in readers:
                with self.subTest(reader=reader):
                    with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                        reader()

    def test_final_delivery_validators_reject_hardlinked_files(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            final_mod = root / "out" / "Fixture" / "final_mod"
            final_mod.mkdir(parents=True)
            os.link(outside, final_mod / "linked.bin")

            for reader in (
                lambda: validate_final_mod.scan_final_mod_tree(final_mod),
                lambda: validate_chs_package.final_files(final_mod),
            ):
                with self.subTest(reader=reader):
                    with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                        reader()

    def test_regular_file_limit_stops_after_requested_result_count(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "b.txt").write_text("b", encoding="utf-8")

            self.assertEqual(
                len(discover_regular_files(root, label="bounded input", max_files=1)),
                1,
            )

    def test_package_validator_rejects_hardlinked_archive_before_open(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.zip"
            outside.write_bytes(b"not-opened")
            package = root / "out" / "Fixture" / "Fixture_CHS.zip"
            package.parent.mkdir(parents=True)
            os.link(outside, package)

            with mock.patch.object(
                validate_chs_package,
                "package_files",
                side_effect=AssertionError("unsafe package was opened"),
            ):
                _rows, issues, *_rest = validate_chs_package.validate_with_intermediate(
                    root,
                    "Fixture",
                    root / "out" / "Fixture" / "final_mod",
                    package,
                )

            self.assertTrue(
                any("hardlink" in issue.Message.casefold() for issue in issues),
                issues,
            )

    def test_workflow_and_qa_readers_reject_hardlinked_pex(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.pex"
            outside.write_bytes(b"outside-pex")
            workspace = root / "work" / "extracted_mods" / "Fixture" / "Scripts"
            workspace.mkdir(parents=True)
            os.link(outside, workspace / "linked.pex")

            readers = (
                lambda: audit_pex_delivery.pex_map(workspace),
                lambda: run_non_gui_qa_gates.collect_final_plugins(workspace),
                lambda: run_non_gui_translation_workflow.run_pex_translation_stage(
                    root,
                    [],
                    [],
                    "Fixture",
                    workspace,
                    load_game_profile("skyrim-se"),
                ),
                lambda: scan_placeholders.collect_input_files(workspace),
            )
            for reader in readers:
                with self.subTest(reader=reader):
                    with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                        reader()

    def test_readiness_and_dictionary_sources_reject_hardlinks(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.xml"
            outside.write_text("<SSTXMLRessources/>", encoding="utf-8")
            mod_input = root / "mod" / "Fixture" / "linked.xml"
            mod_input.parent.mkdir(parents=True)
            os.link(outside, mod_input)

            with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                audit_translation_readiness.collect_mod_inputs(root, [])

            mod_input.unlink()
            dictionary_input = (
                root / "translated" / "xtranslator_ready" / "Fixture" / "linked.xml"
            )
            dictionary_input.parent.mkdir(parents=True)
            os.link(outside, dictionary_input)
            with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                build_final_mod.dictionary_source_files(root, "Fixture")

    def test_fixed_readiness_and_dictionary_evidence_reject_hardlinks(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")

            evidence = root / "qa" / "linked.json"
            evidence.parent.mkdir(parents=True)
            os.link(outside, evidence)
            with self.assertRaisesRegex(ValueError, "missing_or_outside_project"):
                audit_translation_readiness._plugin_stage_file(
                    root,
                    "qa/linked.json",
                    allowed_root=root / "qa",
                    error_prefix="plugin_stage_evidence",
                )

            dictionary_dir = (
                root
                / "out"
                / "Fixture"
                / "汉化产出"
                / "intermediate"
                / "translation_text_dictionary"
            )
            dictionary_dir.mkdir(parents=True)
            os.link(outside, dictionary_dir / "manifest.json")
            with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
                translation_dictionary.inspect_translation_dictionary(root, "Fixture")


if __name__ == "__main__":
    unittest.main()
