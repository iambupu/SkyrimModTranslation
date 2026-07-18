from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adapter_result_io import build_result, write_adapter_result  # noqa: E402
from file_utils import sha256_file  # noqa: E402
from localized_delivery import (  # noqa: E402
    LocalizedPublicationTransaction,
    build_composite_receipt,
    discover_localized_tables,
    load_localized_references,
    load_table_export_ids,
    validate_composite_receipt,
    verify_localized_reference_coverage,
    write_json_atomic,
)


class LocalizedDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
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
        component.output_path.parent.mkdir(parents=True)
        component.output_path.write_bytes(b"translated-table")
        coverage = verify_localized_reference_coverage(
            references,
            {"strings": {100, 300}},
        )
        coverage_report = (
            self.root / "qa" / "localized_delivery" / "Example" / "coverage.json"
        )
        write_json_atomic(coverage_report, coverage.payload())
        component.verify_result.parent.mkdir(parents=True)
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
                input_paths=(component.source_path,),
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
