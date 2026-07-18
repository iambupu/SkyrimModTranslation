from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import MappingProxyType, SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import invoke_bethesda_string_table_tool as string_tool  # noqa: E402
from adapter_result_io import read_adapter_result  # noqa: E402
from capability_resolver import CapabilityDecision  # noqa: E402


OPTIONS = MappingProxyType(
    {
        "source_encoding": "windows-1252",
        "target_encoding": "utf-8",
        "source_language": "english",
        "target_language": "chinese",
        "max_entries": 2_000_000,
        "max_file_bytes": 536_870_912,
    }
)


def decision(operation: str, *, supported: bool = True) -> CapabilityDecision:
    return CapabilityDecision(
        supported=supported,
        capability="string_tables",
        operation=operation,
        level="experimental_write" if supported else "inventory_only",
        adapter_id="bethesda-string-tables",
        adapter_options=OPTIONS,
        strict_complete_allowed=False,
        error_code=None if supported else "capability_unsupported",
        reason="fixture decision",
    )


class BethesdaStringTableWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.input_table = (
            self.root
            / "work"
            / "extracted_mods"
            / "Example"
            / "Strings"
            / "Example_english.strings"
        )
        self.input_table.parent.mkdir(parents=True)
        self.input_table.write_bytes(b"source-table")
        self.translation = (
            self.root
            / "translated"
            / "string_tables"
            / "Example"
            / "Example_english.strings.jsonl"
        )
        self.translation.parent.mkdir(parents=True)
        self.translation.write_text('{"schema_version":2}\n', encoding="utf-8")
        config = self.root / "config" / "tools.local.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        self.output_table = (
            self.root
            / "out"
            / "Example"
            / "tool_outputs"
            / "Strings"
            / "Example_chinese.strings"
        )
        self.calls: list[list[str]] = []

    def _fake_subprocess(self, command, **_kwargs):
        args = [str(value) for value in command]
        self.calls.append(args)

        def value(flag: str) -> Path:
            return Path(args[args.index(flag) + 1])

        report = value("--report")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "# Adapter\n\n"
            f"- game_id: {args[args.index('--game') + 1]}\n"
            f"- capability_level: {args[args.index('--capability-level') + 1]}\n",
            encoding="utf-8",
        )
        if "--output-json" in args:
            output = value("--output-json")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("{}\n", encoding="utf-8")
        if "--output-jsonl" in args:
            output = value("--output-jsonl")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"schema_version":2}\n', encoding="utf-8")
        if args[2] == "apply":
            output = value("--output-table")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"translated-table")
        return CompletedProcess(args, 0)

    def _patches(self):
        return (
            mock.patch.object(string_tool, "project_root", return_value=self.root),
            mock.patch.object(string_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(
                string_tool,
                "resolve_workspace_game_context",
                return_value=SimpleNamespace(game_id="skyrim-se"),
            ),
            mock.patch.object(
                string_tool,
                "resolve_capability",
                side_effect=lambda _context, _capability, operation: decision(operation),
            ),
            mock.patch.object(string_tool, "configured_dotnet_path", return_value=Path("dotnet")),
            mock.patch.object(string_tool, "ensure_adapter_dll", return_value=Path("adapter.dll")),
            mock.patch.object(string_tool.subprocess, "run", side_effect=self._fake_subprocess),
        )

    def _run(self, arguments: list[str]) -> int:
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with mock.patch.object(sys, "argv", ["tool", *arguments]):
                return string_tool.main()

    def test_export_injects_profile_source_settings_and_writes_receipt(self) -> None:
        output = self.root / "source" / "string_tables" / "Example" / "rows.jsonl"
        receipt = self.root / "qa" / "export.adapter_result.json"
        exit_code = self._run(
            [
                "--mode", "Export",
                "--input-table-path", str(self.input_table),
                "--output-jsonl-path", str(output),
                "--report-path", "qa/export.md",
                "--adapter-result-path", str(receipt),
            ]
        )

        self.assertEqual(0, exit_code)
        command = self.calls[0]
        self.assertEqual("windows-1252", command[command.index("--source-encoding") + 1])
        self.assertEqual("english", command[command.index("--source-language") + 1])
        self.assertNotIn("--target-encoding", command)
        result = read_adapter_result(receipt)
        self.assertEqual("extract", result.operation)
        self.assertEqual("bethesda-string-tables", result.adapter_id)
        self.assertEqual("Example", result.mod_name)
        self.assertEqual(1, len(result.inputs))
        self.assertTrue(result.inputs[0].path.endswith("Example_english.strings"))
        self.assertTrue(any(item.path.endswith("rows.jsonl") for item in result.artifacts))

    def test_apply_and_verify_bind_profile_settings_and_apply_receipt(self) -> None:
        apply_receipt = self.root / "qa" / "apply.adapter_result.json"
        apply_exit = self._run(
            [
                "--mode", "Apply",
                "--input-table-path", str(self.input_table),
                "--translation-jsonl-path", str(self.translation),
                "--output-table-path", str(self.output_table),
                "--report-path", "qa/apply.md",
                "--adapter-result-path", str(apply_receipt),
                "--allow-experimental-writeback",
            ]
        )
        self.assertEqual(0, apply_exit)
        apply_command = self.calls[-1]
        self.assertEqual("windows-1252", apply_command[apply_command.index("--source-encoding") + 1])
        self.assertEqual("utf-8", apply_command[apply_command.index("--target-encoding") + 1])
        self.assertEqual("chinese", apply_command[apply_command.index("--target-language") + 1])
        apply_result = read_adapter_result(apply_receipt)
        self.assertEqual("Example", apply_result.mod_name)
        self.assertEqual(2, len(apply_result.inputs))
        self.assertTrue(apply_result.warnings)

        verify_receipt = self.root / "qa" / "verify.adapter_result.json"
        verify_exit = self._run(
            [
                "--mode", "Verify",
                "--input-table-path", str(self.input_table),
                "--translation-jsonl-path", str(self.translation),
                "--output-table-path", str(self.output_table),
                "--report-path", "qa/verify.md",
                "--adapter-result-path", str(verify_receipt),
                "--apply-adapter-result-path", str(apply_receipt),
            ]
        )
        self.assertEqual(0, verify_exit)
        verify_result = read_adapter_result(verify_receipt)
        self.assertEqual("verify", verify_result.operation)
        self.assertTrue(any(item.path.endswith("Example_chinese.strings") for item in verify_result.artifacts))

    def test_unsupported_capability_blocks_before_adapter_invocation(self) -> None:
        receipt = self.root / "qa" / "blocked.adapter_result.json"
        patches = self._patches()
        with patches[0], patches[1], patches[2], mock.patch.object(
            string_tool,
            "resolve_capability",
            return_value=decision("read", supported=False),
        ), patches[4], patches[5], patches[6]:
            with mock.patch.object(
                sys,
                "argv",
                [
                    "tool",
                    "--mode", "Export",
                    "--input-table-path", str(self.input_table),
                    "--output-jsonl-path", str(
                        self.root / "source" / "string_tables" / "Example" / "rows.jsonl"
                    ),
                    "--adapter-result-path", str(receipt),
                ],
            ):
                exit_code = string_tool.main()

        self.assertEqual(2, exit_code)
        self.assertEqual([], self.calls)
        result = read_adapter_result(receipt)
        self.assertEqual("blocked", result.status)
        self.assertEqual("capability_unsupported", result.error_code)

    def test_verify_rejects_stale_apply_output_hash(self) -> None:
        apply_receipt = self.root / "qa" / "apply.adapter_result.json"
        self.assertEqual(
            0,
            self._run(
                [
                    "--mode", "Apply",
                    "--input-table-path", str(self.input_table),
                    "--translation-jsonl-path", str(self.translation),
                    "--output-table-path", str(self.output_table),
                    "--report-path", "qa/apply.md",
                    "--adapter-result-path", str(apply_receipt),
                    "--allow-experimental-writeback",
                ]
            ),
        )
        self.output_table.write_bytes(b"stale")
        verify_receipt = self.root / "qa" / "verify.adapter_result.json"
        self.assertEqual(
            1,
            self._run(
                [
                    "--mode", "Verify",
                    "--input-table-path", str(self.input_table),
                    "--translation-jsonl-path", str(self.translation),
                    "--output-table-path", str(self.output_table),
                    "--report-path", "qa/verify.md",
                    "--adapter-result-path", str(verify_receipt),
                    "--apply-adapter-result-path", str(apply_receipt),
                ]
            ),
        )
        self.assertEqual(1, len(self.calls))
        self.assertEqual("error", read_adapter_result(verify_receipt).status)


if __name__ == "__main__":
    unittest.main()
