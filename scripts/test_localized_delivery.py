from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adapter_result_io import (  # noqa: E402
    build_result,
    require_translation_input_lane,
    write_adapter_result,
)
from file_utils import sha256_file, write_jsonl_sorted  # noqa: E402
import invoke_bethesda_localized_delivery as localized_invoke  # noqa: E402
from invoke_bethesda_localized_delivery import (  # noqa: E402
    _remove_stage_roots,
    _snapshot_translation_components,
    _translated_target_light_state,
    _validate_translation_snapshots,
    _write_referenced_review_input,
)
from localized_delivery import (  # noqa: E402
    LocalizedCoverage,
    LocalizedPublicationTransaction,
    LocalizedTableComponent,
    build_composite_receipt,
    discover_localized_tables,
    load_localized_references,
    load_table_export_ids,
    load_table_translation_ids,
    validate_composite_receipt,
    verify_localized_reference_coverage,
    write_json_atomic,
)
from proofread_translation import proofread_file  # noqa: E402


class LocalizedDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        # GitHub Windows may expose TEMP through an 8.3 alias (RUNNER~1), while
        # resolved child paths use the long user profile name. Canonicalize the
        # fixture root once so identity and relative-path assertions agree.
        self.root = Path(self._temporary.name).resolve(strict=True)
        self.data_root = self.root / "work" / "extracted_mods" / "Example"
        self.strings = self.data_root / "Strings"
        self.strings.mkdir(parents=True)
        self.plugin = self.data_root / "Example.esl"
        self.plugin.write_bytes(b"localized-plugin-anchor")
        self.references_path = (
            self.root
            / "source"
            / "localized_delivery"
            / "Example"
            / "Example.references.jsonl"
        )
        self.references_path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_review_input_includes_all_changes_but_excludes_unreferenced_empty_rows(
        self,
    ) -> None:
        source_table = self.strings / "Example_english.strings"
        source_table.write_bytes(b"source-table")
        translation = (
            self.root
            / "translated"
            / "string_tables"
            / "Example"
            / "Example_english.strings.zh.jsonl"
        )
        translation.parent.mkdir(parents=True)
        translation.write_text(
            json.dumps(
                {"string_id": 100, "Source": "Sword", "Result": "剑"},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {"string_id": 200, "Source": "Unused", "Result": ""},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {"string_id": 300, "Source": "Orphan change", "Result": "孤立改写"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        component = LocalizedTableComponent(
            table_type="strings",
            source_path=source_table,
            output_path=self.root / "out" / "Example" / "tool_outputs" / "Strings" / "Example_chinese.strings",
            export_jsonl=self.root / "source" / "localized_delivery" / "Example" / "Example_english.strings.jsonl",
            translation_jsonl=translation,
            apply_result=self.root / "qa" / "Example.apply.json",
            verify_result=self.root / "qa" / "Example.verify.json",
        )
        coverage = LocalizedCoverage(
            reference_count=1,
            resolved_count=1,
            referenced_ids={"strings": (100,)},
            translated_ids={"strings": (100, 300)},
            missing=(),
        )
        review = (
            self.root
            / "translated"
            / "Example"
            / "localized_delivery"
            / "Example.esp.referenced-translations.jsonl"
        )

        _write_referenced_review_input(
            root=self.root,
            destination=review,
            components=(component,),
            coverage=coverage,
        )

        rows = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["string_id"] for row in rows], [100, 300])
        findings = []
        self.assertEqual(proofread_file(self.root, review, findings, set()), 2)
        self.assertEqual(findings, [])

    def test_translation_snapshot_is_immutable_after_authoring_input_changes(self) -> None:
        source_table = self.strings / "Example_english.strings"
        source_table.write_bytes(b"source-table")
        translation = (
            self.root
            / "translated"
            / "string_tables"
            / "Example"
            / "Example_english.strings.zh.jsonl"
        )
        translation.parent.mkdir(parents=True)
        translated_row = '{"string_id":100,"Source":"Sword","Result":"剑"}\n'
        translation.write_text(translated_row, encoding="utf-8")
        component = LocalizedTableComponent(
            table_type="strings",
            source_path=source_table,
            output_path=self.root / "out" / "Example" / "tool_outputs" / "Strings" / "Example_chinese.strings",
            export_jsonl=self.root / "source" / "localized_delivery" / "Example" / "Example_english.strings.jsonl",
            translation_jsonl=translation,
            apply_result=self.root / "qa" / "Example.apply.json",
            verify_result=self.root / "qa" / "Example.verify.json",
        )

        (snapshot_component,) = _snapshot_translation_components(
            root=self.root,
            mod_name="Example",
            plugin=self.plugin,
            components=(component,),
        )
        translation.write_text(
            '{"string_id":100,"Source":"Sword","Result":""}\n',
            encoding="utf-8",
        )

        self.assertNotEqual(snapshot_component.translation_jsonl, translation)
        require_translation_input_lane(
            self.root,
            snapshot_component.translation_jsonl,
            "Example",
        )
        self.assertEqual(
            snapshot_component.translation_jsonl.read_text(encoding="utf-8"),
            translated_row,
        )
        _validate_translation_snapshots((snapshot_component,))
        snapshot_component.translation_jsonl.write_text(
            '{"string_id":100,"Source":"Sword","Result":""}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "changed after coverage"):
            _validate_translation_snapshots((snapshot_component,))

    def test_target_light_state_ignores_untranslated_unknown_owner(self) -> None:
        references = (
            SimpleNamespace(
                table_type="strings",
                string_id=100,
                master_style="full",
            ),
            SimpleNamespace(
                table_type="strings",
                string_id=200,
                master_style="unknown",
            ),
        )
        coverage = LocalizedCoverage(
            reference_count=1,
            resolved_count=1,
            referenced_ids={"strings": (100, 200)},
            translated_ids={"strings": (100,)},
            missing=(),
        )

        self.assertIs(
            _translated_target_light_state(references, coverage),
            False,
        )

    def test_target_light_state_keeps_unknown_when_mixed_with_light(self) -> None:
        coverage = LocalizedCoverage(
            reference_count=2,
            resolved_count=2,
            referenced_ids={"strings": (100, 200)},
            translated_ids={"strings": (100, 200)},
            missing=(),
        )
        light = SimpleNamespace(
            table_type="strings",
            string_id=100,
            master_style="light",
        )
        unknown = SimpleNamespace(
            table_type="strings",
            string_id=200,
            master_style="unknown",
        )

        for references in ((light, unknown), (unknown, light)):
            with self.subTest(order=tuple(item.master_style for item in references)):
                self.assertIs(
                    _translated_target_light_state(references, coverage),
                    None,
                )

    def _reference_row(
        self,
        *,
        table_type: str = "strings",
        string_id: int = 100,
        field_path: str = "Name",
        subrecord_type: str = "FULL",
        occurrence_index: int = 0,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "game_id": "fallout4",
            "file": "Example.esl",
            "plugin": "Example.esl",
            "mod_key": "Example.esl",
            "localized_flag": True,
            "record_type": "WEAP",
            "form_id": "00000800",
            "owner_mod_key": "Example.esl",
            "local_id": 0x800,
            "master_style": "light",
            "master_style_evidence": "workspace-header",
            "field_path": field_path,
            "subrecord_type": subrecord_type,
            "occurrence_index": occurrence_index,
            "table_type": table_type,
            "string_id": string_id,
        }

    def _write_references(self, *rows: dict[str, object]) -> None:
        self.references_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def _write_table_export(self, component, ids: tuple[int, ...]) -> None:
        component.export_jsonl.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "schema_version": 2,
                "game_id": "fallout4",
                "plugin_basename": "Example",
                "table_type": component.table_type,
                "source_language": "en",
                "string_id": value,
                "Source": f"Value {value}",
                "Result": "",
                "source_table_path": str(component.source_path.relative_to(self.root)).replace(
                    "\\", "/"
                ),
                "source_table_sha256": sha256_file(component.source_path).upper(),
            }
            for value in ids
        ]
        component.export_jsonl.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def _components(self, required=("strings",)):
        return discover_localized_tables(
            data_root=self.data_root,
            plugin_path=self.plugin,
            source_language="en",
            target_language="cn",
            mod_name="Example",
            root=self.root,
            required_types=required,
        )

    def _build_valid_composite_receipt(self):
        source = self.strings / "Example_en.strings"
        source.write_bytes(b"source-table")
        self._write_references(self._reference_row())
        references = load_localized_references(
            self.references_path,
            game_id="fallout4",
            plugin_name="Example.esl",
        )
        component = self._components()[0]
        self._write_table_export(component, (100, 300))
        component.translation_jsonl.parent.mkdir(parents=True, exist_ok=True)
        component.translation_jsonl.write_text(
            component.export_jsonl.read_text(encoding="utf-8").replace(
                '"Result": ""',
                '"Result": "译文"',
            ),
            encoding="utf-8",
        )
        component.output_path.parent.mkdir(parents=True)
        component.output_path.write_bytes(b"translated-table")
        translated_ids = load_table_translation_ids(
            component.translation_jsonl,
            root=self.root,
            game_id="fallout4",
            plugin_basename="Example",
            table_type="strings",
            source_language="en",
            source_table=source,
        )
        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 300}},
            {"strings": translated_ids},
        )
        coverage_report = (
            self.root / "qa" / "localized_delivery" / "Example" / "coverage.json"
        )
        write_json_atomic(coverage_report, coverage.payload())
        review_input = (
            self.root
            / "translated"
            / "Example"
            / "localized_delivery"
            / "Example.esl.referenced-translations.jsonl"
        )
        _write_referenced_review_input(
            root=self.root,
            destination=review_input,
            components=(component,),
            coverage=coverage,
        )
        component.apply_result.parent.mkdir(parents=True)
        apply_report = component.apply_result.with_suffix(".md")
        apply_report.write_text("# String table apply fixture\n", encoding="utf-8")
        write_adapter_result(
            component.apply_result,
            build_result(
                root=self.root,
                status="success",
                error_code=None,
                operation="apply",
                adapter_id="bethesda-string-tables",
                artifact_paths=(component.output_path, apply_report),
                evidence_paths=(apply_report,),
                mod_name="Example",
                input_paths=(component.source_path, component.translation_jsonl),
            ),
        )
        write_adapter_result(
            component.verify_result,
            build_result(
                root=self.root,
                status="success",
                error_code=None,
                operation="verify",
                adapter_id="bethesda-string-tables",
                artifact_paths=(component.output_path,),
                evidence_paths=(),
                mod_name="Example",
                input_paths=(
                    component.source_path,
                    component.translation_jsonl,
                    component.apply_result,
                ),
            ),
        )
        receipt_path = (
            self.root / "qa" / "localized_delivery" / "Example" / "composite.json"
        )
        payload = build_composite_receipt(
            root=self.root,
            operation="verify",
            game_id="fallout4",
            mod_name="Example",
            capability_level="experimental_write",
            plugin_path=self.plugin,
            references_path=self.references_path,
            references=references,
            source_language="en",
            target_language="cn",
            components=(component,),
            component_result_paths=(component.verify_result,),
            coverage=coverage,
            coverage_report=coverage_report,
            review_input=review_input,
            evidence_input_hashes={
                path: sha256_file(path)
                for path in (
                    self.plugin,
                    self.references_path,
                    component.source_path,
                    component.export_jsonl,
                    component.translation_jsonl,
                )
            },
            capability_decisions={
                "localized_delivery": {
                    "level": "experimental_write",
                    "adapter_id": "bethesda-localized-delivery",
                },
                "string_tables": {
                    "level": "experimental_write",
                    "adapter_id": "bethesda-string-tables",
                },
            },
        )
        write_json_atomic(receipt_path, payload)
        return receipt_path, component, payload

    def test_inventory_preserves_normal_and_light_identity(self) -> None:
        full = self._reference_row()
        full.update(
            {
                "plugin": "Example.esp",
                "mod_key": "Example.esp",
                "owner_mod_key": "Example.esp",
                "master_style": "full",
                "master_style_evidence": "ordinary-schema-v2",
            }
        )
        path = self.references_path.with_name("normal.references.jsonl")
        path.write_text(json.dumps(full) + "\n", encoding="utf-8")
        normal = load_localized_references(
            path,
            game_id="fallout4",
            plugin_name="Example.esp",
        )

        self._write_references(self._reference_row())
        light = load_localized_references(
            self.references_path,
            game_id="fallout4",
            plugin_name="Example.esl",
        )

        self.assertEqual("full", normal[0].master_style)
        self.assertEqual("light", light[0].master_style)
        self.assertEqual(0x800, light[0].local_id)

    def test_inventory_accepts_unresolved_target_for_scoped_preflight(self) -> None:
        unresolved = self._reference_row()
        unresolved.update(
            {
                "plugin": "Example.esp",
                "mod_key": "Example.esp",
                "owner_mod_key": "CustomMaster.esm",
                "local_id": 0x12345,
                "master_style": "unknown",
                "master_style_evidence": "unresolved:unseparated-master-order",
            }
        )
        path = self.references_path.with_name("unresolved.references.jsonl")
        path.write_text(json.dumps(unresolved) + "\n", encoding="utf-8")

        references = load_localized_references(
            path,
            game_id="fallout4",
            plugin_name="Example.esp",
        )

        self.assertEqual("unknown", references[0].master_style)
        self.assertEqual("CustomMaster.esm", references[0].owner_mod_key)
        self.assertEqual(0x12345, references[0].local_id)

    def test_inventory_rejects_unresolved_target_without_canonical_evidence(self) -> None:
        unresolved = self._reference_row()
        unresolved.update(
            {
                "plugin": "Example.esp",
                "mod_key": "Example.esp",
                "owner_mod_key": "CustomMaster.esm",
                "master_style": "unknown",
                "master_style_evidence": "workspace-header",
            }
        )
        path = self.references_path.with_name("invalid-unresolved.references.jsonl")
        path.write_text(json.dumps(unresolved) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unresolved master-style evidence"):
            load_localized_references(
                path,
                game_id="fallout4",
                plugin_name="Example.esp",
            )

    def test_discovery_requires_exact_basename_language_and_type(self) -> None:
        wrong_language = self.strings / "Example_fr.strings"
        wrong_plugin = self.strings / "Other_en.strings"
        wrong_language.write_bytes(b"wrong-language")
        wrong_plugin.write_bytes(b"wrong-plugin")

        with self.assertRaisesRegex(ValueError, "Example_en.strings"):
            self._components()

        source = self.strings / "Example_en.strings"
        source.write_bytes(b"correct")
        components = self._components()
        self.assertEqual((source,), tuple(item.source_path for item in components))

    def test_reference_coverage_reports_wrong_table_and_missing_id(self) -> None:
        self._write_references(
            self._reference_row(string_id=100),
            self._reference_row(
                table_type="dlstrings",
                string_id=200,
                field_path="Description",
                subrecord_type="DESC",
            ),
        )
        references = load_localized_references(
            self.references_path,
            game_id="fallout4",
            plugin_name="Example.esl",
        )

        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 200}, "dlstrings": set()},
        )

        self.assertFalse(coverage.passed)
        self.assertEqual(1, coverage.resolved_count)
        self.assertEqual("dlstrings", coverage.missing[0]["table_type"])
        self.assertEqual(200, coverage.missing[0]["string_id"])

    def test_reference_coverage_requires_actual_changed_translation(self) -> None:
        source = self.strings / "Example_en.strings"
        source.write_bytes(b"source-table")
        self._write_references(self._reference_row(string_id=100))
        references = load_localized_references(
            self.references_path,
            game_id="fallout4",
            plugin_name="Example.esl",
        )
        component = self._components()[0]
        self._write_table_export(component, (100, 300))
        component.translation_jsonl.parent.mkdir(parents=True, exist_ok=True)

        untranslated = component.export_jsonl.read_text(encoding="utf-8")
        component.translation_jsonl.write_text(untranslated, encoding="utf-8")
        translated_ids = load_table_translation_ids(
            component.translation_jsonl,
            root=self.root,
            game_id="fallout4",
            plugin_basename="Example",
            table_type="strings",
            source_language="en",
            source_table=source,
        )
        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 300}},
            {"strings": translated_ids},
        )
        self.assertFalse(coverage.passed)
        self.assertEqual("translation_missing_or_unchanged", coverage.missing[0]["reason"])

        same_as_source = untranslated.replace(
            '"Result": ""',
            '"Result": "Value 100"',
            1,
        )
        component.translation_jsonl.write_text(same_as_source, encoding="utf-8")
        self.assertEqual(
            frozenset(),
            load_table_translation_ids(
                component.translation_jsonl,
                root=self.root,
                game_id="fallout4",
                plugin_basename="Example",
                table_type="strings",
                source_language="en",
                source_table=source,
            ),
        )

        translated = untranslated.replace(
            '"Result": ""',
            '"Result": "译文"',
            1,
        )
        component.translation_jsonl.write_text(translated, encoding="utf-8")
        translated_ids = load_table_translation_ids(
            component.translation_jsonl,
            root=self.root,
            game_id="fallout4",
            plugin_basename="Example",
            table_type="strings",
            source_language="en",
            source_table=source,
        )
        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 300}},
            {"strings": translated_ids},
        )
        self.assertTrue(coverage.passed)
        self.assertEqual(frozenset({100}), translated_ids)

    def test_source_only_coverage_does_not_claim_translated_ids(self) -> None:
        self._write_references(self._reference_row(string_id=100))
        references = load_localized_references(
            self.references_path,
            game_id="fallout4",
            plugin_name="Example.esl",
        )

        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 300}},
        )

        self.assertTrue(coverage.passed)
        self.assertEqual({}, coverage.translated_ids)
        self.assertEqual({}, coverage.payload()["translated_ids"])

    def test_table_export_identity_and_source_hash_are_bound(self) -> None:
        source = self.strings / "Example_en.strings"
        source.write_bytes(b"source-table")
        component = self._components()[0]
        self._write_table_export(component, (100, 300))

        ids = load_table_export_ids(
            component.export_jsonl,
            root=self.root,
            game_id="fallout4",
            plugin_basename="Example",
            table_type="strings",
            source_language="en",
            source_table=source,
        )
        self.assertEqual(frozenset({100, 300}), ids)

        source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash is stale"):
            load_table_export_ids(
                component.export_jsonl,
                root=self.root,
                game_id="fallout4",
                plugin_basename="Example",
                table_type="strings",
                source_language="en",
                source_table=source,
            )

    def test_composite_receipt_binds_all_component_hashes(self) -> None:
        receipt_path, component, _ = self._build_valid_composite_receipt()

        validated = validate_composite_receipt(self.root, receipt_path)
        self.assertEqual("Example.esl", validated["plugin"]["file_name"])

        component.output_path.write_bytes(b"stale-output")
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_stale_plugin_hash(self) -> None:
        receipt_path, _, _ = self._build_valid_composite_receipt()

        self.plugin.write_bytes(b"changed-plugin-anchor")

        with self.assertRaisesRegex(ValueError, "stale.*Example.esl"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_stale_translation_jsonl(self) -> None:
        receipt_path, component, _ = self._build_valid_composite_receipt()

        component.translation_jsonl.write_text('{"changed":true}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, r"stale: .*strings\.jsonl|input lineage"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_stale_review_input(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        review_input = self.root / payload["review_input"]["path"]

        review_input.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "stale"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_review_content_tamper(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        review_input = self.root / payload["review_input"]["path"]
        rows = [
            json.loads(line)
            for line in review_input.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows[0]["Source"] = "Benign source"
        rows[0]["Result"] = "Benign result"
        write_jsonl_sorted(review_input, rows)
        payload["review_input"]["sha256"] = sha256_file(review_input)
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "review input"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_reference_summary_tamper(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        payload["references"]["count"] = 999
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "reference summary"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_source_table_summary_tamper(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        payload["source_tables"][0]["translated_ids"] = [999]
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "source table summary"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_coverage_report_content_tamper(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        report_binding = payload["coverage"]["report"]
        report = self.root / report_binding["path"]
        report_payload = json.loads(report.read_text(encoding="utf-8"))
        report_payload["reference_count"] = 999
        write_json_atomic(report, report_payload)
        report_binding["sha256"] = sha256_file(report)
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "coverage report"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_pre_translation_coverage_schema(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        self.assertEqual(3, payload["schema_version"])
        payload["schema_version"] = 2
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "schema_version"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_recomputes_coverage_from_bound_inputs(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        reference = self._reference_row(string_id=999)
        self._write_references(reference)
        payload["references"]["sha256"] = sha256_file(self.references_path)
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_composite_receipt(self.root, receipt_path)

    def test_localized_evidence_guard_detects_post_coverage_replacement(self) -> None:
        receipt_path, component, _ = self._build_valid_composite_receipt()
        bindings = localized_invoke._capture_localized_evidence_inputs(
            (self.plugin, self.references_path, component.export_jsonl)
        )
        self.references_path.write_text(
            json.dumps(self._reference_row(string_id=999)) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "changed after coverage"):
            localized_invoke._validate_localized_evidence_inputs(bindings)

    def test_localized_lane_lock_reuses_verified_scheduler_lock(self) -> None:
        from workflow_lock import (
            RESOURCE_LOCKS_ENV,
            ResourceLock,
            resource_lock_environment,
        )

        outer = ResourceLock(self.root, "mod:Example", "scheduler").acquire()
        old_value = os.environ.get(RESOURCE_LOCKS_ENV)
        try:
            os.environ[RESOURCE_LOCKS_ENV] = resource_lock_environment((outer,))
            inner = localized_invoke._acquire_localized_lane_lock(
                self.root,
                "Example",
            )
            self.assertTrue(inner.reentrant)
            inner.release()
            self.assertTrue(outer.path.is_file())
        finally:
            if old_value is None:
                os.environ.pop(RESOURCE_LOCKS_ENV, None)
            else:
                os.environ[RESOURCE_LOCKS_ENV] = old_value
            outer.release()

    def test_stage_cleanup_rejects_reparse_root_without_deleting_target(self) -> None:
        victim = self.root / "work" / "cleanup-victim"
        victim.mkdir(parents=True)
        marker = victim / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        stage = (
            self.root
            / "out"
            / "Example"
            / "tool_outputs"
            / ".localized-staging-test"
        )
        stage.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(victim, stage, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "symlink|reparse|junction"):
            _remove_stage_roots(self.root, (stage,))

        self.assertTrue(marker.is_file())

    def test_stage_cleanup_rejects_unexpected_directory_name(self) -> None:
        unexpected = self.root / "out" / "Example" / "tool_outputs" / "published"
        unexpected.mkdir(parents=True)
        marker = unexpected / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "staging path"):
            _remove_stage_roots(self.root, (unexpected,))

        self.assertTrue(marker.is_file())

    def test_localized_plugin_stem_collision_is_rejected(self) -> None:
        conflicting = self.data_root / "Example.esp"
        conflicting.write_bytes(b"second-localized-plugin")

        with self.assertRaisesRegex(ValueError, "string-table basename collision"):
            localized_invoke._require_unique_localized_plugin_stem(
                self.data_root,
                self.plugin,
            )

    def test_composite_receipt_rejects_component_missing_translation_input(self) -> None:
        receipt_path, component, payload = self._build_valid_composite_receipt()
        write_adapter_result(
            component.verify_result,
            build_result(
                root=self.root,
                status="success",
                error_code=None,
                operation="verify",
                adapter_id="bethesda-string-tables",
                artifact_paths=(component.output_path,),
                evidence_paths=(),
                mod_name="Example",
                input_paths=(component.source_path, component.apply_result),
            ),
        )
        payload["component_adapter_results"][0]["sha256"] = sha256_file(
            component.verify_result
        )
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "input lineage"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_hardlinked_bound_component(self) -> None:
        receipt_path, component, _ = self._build_valid_composite_receipt()
        outside = self.root / "outside-translated-table"
        outside.write_bytes(component.output_path.read_bytes())
        component.output_path.unlink()
        os.link(outside, component.output_path)

        with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
            validate_composite_receipt(self.root, receipt_path)

    def test_table_discovery_rejects_hardlinked_source_component(self) -> None:
        source = self.data_root / "Strings" / "Example_en.strings"
        source.write_bytes(b"source-table")
        outside = self.root / "outside.strings"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        os.link(outside, source)

        with self.assertRaisesRegex(ValueError, "hardlink|multiple hardlinks"):
            self._components()

    def test_composite_receipt_rejects_partial_publication(self) -> None:
        receipt_path, _, payload = self._build_valid_composite_receipt()
        payload["output_tables"] = []
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "partial table publication"):
            validate_composite_receipt(self.root, receipt_path)

    def test_composite_receipt_rejects_conflicting_component_result(self) -> None:
        receipt_path, component, payload = self._build_valid_composite_receipt()
        other_output = (
            self.root
            / "out"
            / "Example"
            / "tool_outputs"
            / "Strings"
            / "Other_cn.strings"
        )
        other_output.write_bytes(b"other-output")
        conflicting_result = receipt_path.with_name("conflicting.adapter-result.json")
        write_adapter_result(
            conflicting_result,
            build_result(
                root=self.root,
                status="success",
                error_code=None,
                operation="verify",
                adapter_id="bethesda-string-tables",
                artifact_paths=(other_output,),
                mod_name="Example",
                input_paths=(component.source_path,),
            ),
        )
        payload["component_adapter_results"] = [
            {
                "table_type": "strings",
                "path": conflicting_result.relative_to(self.root).as_posix(),
                "sha256": sha256_file(conflicting_result),
            }
        ]
        write_json_atomic(receipt_path, payload)

        with self.assertRaisesRegex(ValueError, "does not bind its expected output"):
            validate_composite_receipt(self.root, receipt_path)

    def test_publication_transaction_restores_previous_files_on_failure(self) -> None:
        output = self.root / "out" / "Example" / "tool_outputs" / "Strings" / "Example_cn.strings"
        receipt = self.root / "qa" / "localized_delivery" / "Example" / "receipt.json"
        staged_output = self.root / "out" / "Example" / "tool_outputs" / ".stage" / "Example_cn.strings"
        staged_receipt = self.root / "qa" / "localized_delivery" / "Example" / ".stage" / "receipt.json"
        for path, content in (
            (output, b"old-output"),
            (receipt, b"old-receipt"),
            (staged_output, b"new-output"),
            (staged_receipt, b"new-receipt"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        with self.assertRaisesRegex(RuntimeError, "publication failed"):
            with LocalizedPublicationTransaction(self.root, "Example") as transaction:
                transaction.publish(staged_output, output)
                transaction.publish(staged_receipt, receipt)
                raise RuntimeError("publication failed")

        self.assertEqual(b"old-output", output.read_bytes())
        self.assertEqual(b"old-receipt", receipt.read_bytes())
        transaction_root = self.root / "work" / "localized_delivery_transactions" / "Example"
        self.assertFalse(transaction_root.exists() and any(transaction_root.rglob("*")))

    def test_publication_transaction_rejects_symlink_destination(self) -> None:
        victim = self.root / "work" / "publication-victim.txt"
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_bytes(b"keep")
        destination = (
            self.root
            / "out"
            / "Example"
            / "tool_outputs"
            / "Strings"
            / "Example_cn.strings"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = (
            self.root
            / "out"
            / "Example"
            / "tool_outputs"
            / ".stage"
            / "Example_cn.strings"
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"replace")
        try:
            os.symlink(victim, destination)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        transaction = LocalizedPublicationTransaction(self.root, "Example")
        try:
            with self.assertRaisesRegex(ValueError, "symlink|reparse|junction"):
                transaction.publish(staged, destination)
        finally:
            transaction.rollback()

        self.assertEqual(b"keep", victim.read_bytes())
        self.assertTrue(destination.is_symlink())

    def test_publication_transaction_commits_complete_file_set(self) -> None:
        output = self.root / "out" / "Example" / "tool_outputs" / "Strings" / "Example_cn.strings"
        receipt = self.root / "qa" / "localized_delivery" / "Example" / "receipt.json"
        staged_output = self.root / "out" / "Example" / "tool_outputs" / ".stage" / "Example_cn.strings"
        staged_receipt = self.root / "qa" / "localized_delivery" / "Example" / ".stage" / "receipt.json"
        for path, content in (
            (output, b"old-output"),
            (receipt, b"old-receipt"),
            (staged_output, b"new-output"),
            (staged_receipt, b"new-receipt"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        with LocalizedPublicationTransaction(self.root, "Example") as transaction:
            transaction.publish(staged_output, output)
            transaction.publish(staged_receipt, receipt)
            transaction.commit()

        self.assertEqual(b"new-output", output.read_bytes())
        self.assertEqual(b"new-receipt", receipt.read_bytes())
        transaction_root = self.root / "work" / "localized_delivery_transactions" / "Example"
        self.assertFalse(transaction_root.exists() and any(transaction_root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
