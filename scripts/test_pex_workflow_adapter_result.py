from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_non_gui_translation_workflow as workflow
from game_context import resolve_workspace_game_context


class PexWorkflowAdapterResultTests(unittest.TestCase):
    def test_apply_requests_adapter_result_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod_name = "TestMod"
            workspace = root / "work" / "extracted_mods" / mod_name
            pex = workspace / "Scripts" / "Test.pex"
            pex.parent.mkdir(parents=True)
            pex.write_bytes(b"fixture")
            marker = {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "plugin_name": "skyrim-mod-chs-translation",
                "plugin_root": str(Path(__file__).resolve().parents[1]),
                "game_id": "skyrim-se",
                "game_profile": "skyrim-se",
            }
            (root / ".skyrim-chs-workspace.json").write_text(
                json.dumps(marker), encoding="utf-8"
            )
            translation = root / "work" / "normalized" / mod_name / "pex_visible_strings" / "Test.translation.jsonl"
            translation.parent.mkdir(parents=True)
            translation.write_text(
                json.dumps(
                    {
                        "ModName": "Test.pex",
                        "Source": "Visible notification text",
                        "Result": "可见通知文本",
                        "risk": "candidate",
                        "opcode": "CALLMETHOD",
                        "notes": "Agent model confirmed player-visible from PSC call context.",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            calls: list[tuple[str, list[str]]] = []

            def successful_run(_root: Path, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append((script, args))
                return subprocess.CompletedProcess([script], 0, stdout="ok\n", stderr="")

            steps: list[workflow.Step] = []
            issues: list[workflow.Issue] = []
            with (
                mock.patch.object(workflow, "collect_pex_translation_inputs", return_value=[translation]),
                mock.patch.object(workflow, "run_python_script", side_effect=successful_run),
            ):
                self.assertTrue(
                    workflow.run_pex_translation_stage(
                        root,
                        steps,
                        issues,
                        mod_name,
                        workspace,
                        resolve_workspace_game_context(root),
                    )
                )

            apply_args = next(
                args
                for script, args in calls
                if script == "invoke_mutagen_pex_string_tool.py" and args[args.index("--mode") + 1] == "Apply"
            )
            receipt = apply_args[apply_args.index("--adapter-result-path") + 1]
            self.assertEqual(
                receipt.replace("/", "\\"),
                f"qa\\{mod_name}.Test.pex_apply.adapter_result.json",
            )


if __name__ == "__main__":
    unittest.main()
