from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extract_non_gui_candidates import (  # noqa: E402
    classify_string,
    extract_interface_translation,
    extract_json,
    extract_psc,
    extract_xml,
)


class ExtractNonGuiCandidatesTests(unittest.TestCase):
    def test_uppercase_text_in_visible_context_is_not_hidden_as_an_acronym(self) -> None:
        self.assertEqual(
            classify_string("RESET", "label"),
            ("candidate", "visible-field-context"),
        )

    def test_interface_uppercase_value_is_a_translation_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            translations = root / "Interface" / "Translations" / "Fixture_english.txt"
            translations.parent.mkdir(parents=True)
            translations.write_text("$Reset\tRESET\n", encoding="utf-8")

            rows = extract_interface_translation(root, translations)

            self.assertEqual(rows[0]["risk"], "candidate")
            self.assertEqual(rows[0]["reason"], "visible-field-context")

    def test_fomod_module_config_extracts_visible_labels_but_protects_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module_config = root / "fomod" / "ModuleConfig.xml"
            module_config.parent.mkdir(parents=True)
            module_config.write_text(
                """<?xml version=\"1.0\" encoding=\"utf-8\"?>
<config>
  <moduleName>EXAMPLE INSTALLER</moduleName>
  <installSteps>
    <installStep name=\"MAIN OPTIONS\">
      <optionalFileGroups>
        <group name=\"FEATURES\">
          <plugins>
            <plugin name=\"ENABLE WIDGET\">
              <description>ENABLE THE VISIBLE WIDGET</description>
              <files><file source=\"Data\\Widget.esp\" destination=\"Widget.esp\" /></files>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
""",
                encoding="utf-8",
            )

            rows = extract_xml(root, module_config)
            by_value = {str(row["source"]).strip(): row for row in rows}

            for value in (
                "EXAMPLE INSTALLER",
                "MAIN OPTIONS",
                "FEATURES",
                "ENABLE WIDGET",
                "ENABLE THE VISIBLE WIDGET",
            ):
                self.assertEqual(by_value[value]["risk"], "candidate", value)
            self.assertEqual(by_value["Data\\Widget.esp"]["risk"], "protected")
            self.assertEqual(by_value["Widget.esp"]["risk"], "protected")

    def test_mcm_mod_name_metadata_is_protected_not_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "work" / "extracted_mods" / "Fixture" / "MCM" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"modName": "Codex Fixture", "title": "Open Expression Menu"}),
                encoding="utf-8",
            )

            rows = {row["json_path"]: row for row in extract_json(root, config)}

            self.assertEqual(rows["modName"]["risk"], "protected")
            self.assertEqual(rows["modName"]["reason"], "protected-json-key")
            self.assertEqual(rows["title"]["risk"], "candidate")

    def test_non_mcm_source_and_type_values_remain_translation_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "work" / "extracted_mods" / "Fixture" / "Interface" / "text.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"source": "Open the ancient door", "type": "Quest objective text"}),
                encoding="utf-8",
            )

            rows = {row["json_path"]: row for row in extract_json(root, config)}

            self.assertEqual(rows["source"]["risk"], "candidate")
            self.assertEqual(rows["type"]["risk"], "candidate")

    def test_mod_name_containing_mcm_does_not_make_json_mcm_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "work" / "extracted_mods" / "MCMHelperAddon" / "Interface" / "text.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"source": "Open the hidden panel", "type": "Quest objective text"}),
                encoding="utf-8",
            )

            rows = {row["json_path"]: row for row in extract_json(root, config)}

            self.assertEqual(rows["source"]["risk"], "candidate")
            self.assertEqual(rows["type"]["risk"], "candidate")

    def test_mcm_source_and_type_metadata_stays_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "work" / "extracted_mods" / "Fixture" / "MCM" / "settings.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"source": "StorageUtil", "type": "slider", "title": "Voice volume"}),
                encoding="utf-8",
            )

            rows = {row["json_path"]: row for row in extract_json(root, config)}

            self.assertEqual(rows["source"]["risk"], "protected")
            self.assertEqual(rows["type"]["risk"], "protected")
            self.assertEqual(rows["title"]["risk"], "candidate")

    def test_psc_string_decoder_preserves_unicode_and_known_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "work" / "extracted_mods" / "Fixture" / "Scripts" / "Source" / "Example.psc"
            source.parent.mkdir(parents=True)
            source.write_text(
                'Debug.Notification("已启用\\nPress \\"OK\\"")\n',
                encoding="utf-8",
            )

            rows = extract_psc(root, source)

            self.assertEqual(rows[0]["source"], '已启用\nPress "OK"')
            self.assertNotIn("�", rows[0]["source"])


if __name__ == "__main__":
    unittest.main()
