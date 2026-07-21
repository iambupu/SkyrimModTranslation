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
import zipfile
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from test_fallout4_plugin_adapter_regressions import DOTNET  # noqa: E402
from test_fallout4_pex_adapter_regressions import FIXTURE_PROJECT, FIXTURE_SOURCE  # noqa: E402
from adapter_result_io import build_result, write_adapter_result  # noqa: E402
from build_final_mod import source_hash as final_source_hash  # noqa: E402
from game_context import GAME_METADATA_KEYS, game_context_metadata, load_game_profile  # noqa: E402
import run_plugin_translation_stage as plugin_stage  # noqa: E402
import plugin_resource_evidence  # noqa: E402
import run_non_gui_qa_gates as strict_qa  # noqa: E402
import audit_translation_readiness as readiness  # noqa: E402
from audit_translation_readiness import plugin_stage_status  # noqa: E402
from plugin_resource_evidence import validate_plugin_report_status  # noqa: E402
from write_workflow_state import next_actions_from_actions  # noqa: E402
from write_workflow_tasks import build_tasks, task_from_action  # noqa: E402

MOD_NAME = "Classic Holstered Weapons - v1.09-46101-1-09-1779912557"
GAME_KEYS = set(GAME_METADATA_KEYS)
WORKSPACE_SAFE_DOTNET = ROOT / "tools" / "dotnet-sdk" / "dotnet.exe"
if not WORKSPACE_SAFE_DOTNET.is_file():
    WORKSPACE_SAFE_DOTNET = None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_test_ba2(path: Path) -> None:
    entries = (
        ("Interface/translations/Classic_en.txt", b"$HELLO\tHello"),
        ("Materials/classic.bgsm", b"synthetic-material"),
    )
    names = b"".join(
        struct.pack("<H", len(name.encode("utf-8"))) + name.encode("utf-8")
        for name, _payload in entries
    )
    names_offset = 24 + 36 * len(entries)
    cursor = names_offset + len(names)
    records: list[bytes] = []
    payloads: list[bytes] = []
    for _name, payload in entries:
        records.append(struct.pack("<I4sIIQIII", 0, b"\0\0\0\0", 0, 0, cursor, 0, len(payload), 0))
        payloads.append(payload)
        cursor += len(payload)
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"GNRL", len(entries), names_offset)
        + b"".join(records)
        + names
        + b"".join(payloads)
    )


class Fallout4WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = ROOT / ".tmp" / "test-fallout4-workflow-integration"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.tempdir.cleanup)
        self.reset_workspace("workspace")

    def reset_workspace(self, name: str) -> None:
        self.workspace = Path(self.tempdir.name) / name
        self.workspace.mkdir()
        for name in ("mod", "work", "qa", "out", "source", "translated", "glossary", ".workflow", "traces", "config"):
            (self.workspace / name).mkdir(parents=True, exist_ok=True)

    def write_marker(self, game_id: str | None) -> None:
        marker: dict[str, object] = {
            "schema_version": 2 if game_id else 1,
            "kind": "bethesda-mod-chs-translation-workspace" if game_id else "skyrim-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "plugin_root": str(ROOT),
        }
        if game_id:
            marker["game_id"] = game_id
            marker["game_profile"] = game_id
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["SKYRIM_CHS_WORKSPACE_ROOT"] = str(self.workspace)
        env["SKYRIM_CHS_PLUGIN_ROOT"] = str(ROOT)
        env.update(getattr(self, "extra_env", {}))
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            cwd=self.workspace,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def read_json(self, relative: str) -> dict[str, object]:
        return json.loads((self.workspace / relative).read_text(encoding="utf-8-sig"))

    def assert_game_metadata(self, payload: dict[str, object], game_id: str) -> None:
        self.assertTrue(GAME_KEYS.issubset(payload), sorted(GAME_KEYS - set(payload)))
        self.assertEqual(payload["game_id"], game_id)
        if game_id == "fallout4":
            self.assertEqual(payload["game_display_name"], "Fallout 4")
            self.assertEqual(payload["support_level"], "experimental")
            self.assertEqual(payload["interface_translation_encoding"], "utf-16-le-bom")

    def run_state_chain(self) -> None:
        commands = (
            ("audit_translation_readiness.py",),
            ("write_workflow_state.py",),
            ("write_workflow_tasks.py",),
            ("write_codex_handoff.py",),
            ("write_agent_handoff.py",),
        )
        for command in commands:
            result = self.run_script(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_dictionary(self, mod_name: str = MOD_NAME) -> None:
        dictionary = self.workspace / "out" / mod_name / "lex_dictionary" / "entries.jsonl"
        dictionary.parent.mkdir(parents=True, exist_ok=True)
        dictionary.write_text(
            json.dumps({"source": "Visible text", "target": "可见文本"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def run_mocked_plugin_stage(
        self,
        plugin_name: str,
        *,
        localized: str = "false",
        light_by_extension: str = "false",
        light_by_header: str = "false",
        targets_light_owner: str = "false",
        contains_unsupported_light_formids: str = "false",
        export_returncode: int = 0,
        export_status: str | None = None,
        omit_export_status: bool = False,
        duplicate_export_status: bool = False,
        output_export_returncode: int = 0,
        output_export_status: str | None = None,
        omit_output_export_status: bool = False,
        duplicate_output_export_status: bool = False,
        apply_status: str | None = "ready",
        apply_returncode: int = 0,
        duplicate_apply_status: bool = False,
        verify_status: str | None = "ready",
        verify_returncode: int = 0,
        duplicate_verify_status: bool = False,
        post_verify_overrides: dict[str, str] | None = None,
        omitted_traits: frozenset[str] = frozenset(),
        additional_plugins: tuple[str, ...] = (),
        omitted_identity_fields: frozenset[str] = frozenset(),
        seed_stale_generated: bool = False,
        map_mode: str = "keyed",
        keyed_map_payload: str = '{"schema_version": 2, "translations": []}\n',
        legacy_map_payload: str = '{"schema_version": 2, "translations": []}\n',
        legacy_receipt_mode: str | None = None,
        legacy_map_transform: Callable[[Path], None] | None = None,
        legacy_receipt_transform: Callable[[Path], None] | None = None,
        has_candidates: bool = True,
        master_name: str | None = None,
        master_manifest_payload: str | None = None,
        materialize_master_style_evidence: bool = False,
        localized_receipt_valid: bool = False,
        unused_unknown_candidate: bool = False,
    ) -> tuple[int, dict[str, object], list[str]]:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / "Example"
        workspace.mkdir(parents=True, exist_ok=True)
        for relative_name in (plugin_name, *additional_plugins):
            plugin_path = workspace / relative_name
            plugin_path.parent.mkdir(parents=True, exist_ok=True)
            header_data = (
                b"MAST"
                + (len(master_name.encode("utf-8")) + 1).to_bytes(2, "little")
                + master_name.encode("utf-8")
                + b"\0"
                if master_name is not None
                else b""
            )
            header = bytearray(
                b"TES4"
                + len(header_data).to_bytes(4, "little")
                + (b"\x00" * 16)
                + header_data
            )
            if light_by_header == "true":
                header[8:12] = (0x00000200).to_bytes(4, "little")
            plugin_path.write_bytes(header)
            artifact_key = plugin_stage.plugin_artifact_key("Example", Path(relative_name))
            if master_manifest_payload is not None and relative_name == plugin_name:
                manifest = (
                    self.workspace
                    / "work"
                    / "plugin_context"
                    / "Example"
                    / f"{artifact_key}.master-styles.json"
                )
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(master_manifest_payload, encoding="utf-8")
            map_root = self.workspace / "work" / "plugin_translation_maps" / "Example"
            map_root.mkdir(parents=True, exist_ok=True)
            map_names: list[str] = []
            if map_mode in {"keyed", "both"}:
                map_names.append(f"{artifact_key}.translation_map.json")
            if map_mode in {"legacy", "both"}:
                map_names.append(f"{Path(relative_name).name}.translation_map.json")
            for map_name in map_names:
                (map_root / map_name).write_text(
                    (
                        legacy_map_payload
                        if map_name == f"{Path(relative_name).name}.translation_map.json"
                        else keyed_map_payload
                    ),
                    encoding="utf-8",
                )
                if (
                    legacy_map_transform is not None
                    and map_name == f"{Path(relative_name).name}.translation_map.json"
                ):
                    legacy_map_transform(map_root / map_name)
        if master_name is not None and materialize_master_style_evidence:
            master = (
                self.workspace
                / "work"
                / "master_context"
                / "fallout4"
                / master_name
            )
            master.parent.mkdir(parents=True, exist_ok=True)
            master.write_bytes(b"TES4" + (b"\x00" * 20))
            if seed_stale_generated:
                stale_receipt = self.workspace / "qa" / f"{artifact_key}.apply.adapter_result.json"
                stale_receipt.write_text('{"stale":true}\n', encoding="utf-8")
        localized_receipt_payload: dict[str, object] | None = None
        if localized_receipt_valid:
            localized_plugin = workspace / plugin_name
            localized_receipt = (
                self.workspace
                / "qa"
                / "localized_delivery"
                / "Example"
                / f"{plugin_stage.safe_file_name(localized_plugin.name)}.apply.composite.json"
            )
            localized_receipt.parent.mkdir(parents=True, exist_ok=True)
            localized_receipt.write_text('{"fixture":true}\n', encoding="utf-8")
            localized_receipt_payload = {
                "operation": "apply",
                "game_id": "fallout4",
                "mod_name": "Example",
                "plugin": {
                    "path": localized_plugin.relative_to(self.workspace).as_posix(),
                    "sha256": sha256(localized_plugin),
                },
            }
        calls: list[str] = []

        def argument(args: list[str], name: str) -> Path:
            return Path(args[args.index(name) + 1])

        def master_context_for(input_plugin: Path, operation: str) -> Path | None:
            if light_by_extension != "true" and light_by_header != "true":
                return None
            input_relative = input_plugin.relative_to(self.workspace).as_posix()
            context_key = hashlib.sha256(
                f"{input_relative}|{operation}".encode("utf-8")
            ).hexdigest()[:16]
            context = (
                self.workspace
                / "work"
                / "plugin_context"
                / "Example"
                / f"{input_plugin.name}.{operation}.{context_key}.json"
            )
            context.parent.mkdir(parents=True, exist_ok=True)
            master_rows: list[dict[str, object]] = []
            if master_name is not None:
                if Path(master_name).suffix.casefold() == ".esl":
                    master_rows.append(
                        {
                            "mod_key": master_name,
                            "master_style": "light",
                            "evidence_source": "extension:.esl",
                            "inspected_path": None,
                            "inspected_sha256": None,
                            "small_flag": None,
                        }
                    )
                elif master_name.casefold() == "fallout4.esm":
                    master_rows.append(
                        {
                            "mod_key": master_name,
                            "master_style": "full",
                            "evidence_source": "game-profile:known-full",
                            "inspected_path": None,
                            "inspected_sha256": None,
                            "small_flag": None,
                        }
                    )
                elif materialize_master_style_evidence:
                    master = (
                        self.workspace
                        / "work"
                        / "master_context"
                        / "fallout4"
                        / master_name
                    )
                    master_rows.append(
                        {
                            "mod_key": master_name,
                            "master_style": "full",
                            "evidence_source": "fixture:workspace-header",
                            "inspected_path": master.relative_to(self.workspace).as_posix(),
                            "inspected_sha256": sha256(master),
                            "small_flag": False,
                        }
                    )
                else:
                    master_rows.append(
                        {
                            "mod_key": master_name,
                            "master_style": "unknown",
                            "evidence_source": "unresolved:unseparated-master-order",
                            "inspected_path": None,
                            "inspected_sha256": None,
                            "small_flag": None,
                        }
                    )
            context.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "game_id": "fallout4",
                        "plugin": input_plugin.name,
                        "input_path": input_relative,
                        "input_sha256": sha256(input_plugin),
                        "current_style": "light",
                        "current_evidence_source": "fixture:light-plugin",
                        "current_inspected_path": input_relative,
                        "current_inspected_sha256": sha256(input_plugin),
                        "current_small_flag": bool(
                            int.from_bytes(input_plugin.read_bytes()[8:12], "little")
                            & 0x00000200
                        ),
                        "masters": master_rows,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return context

        def report_text(
            status: str | None,
            input_plugin: Path,
            operation: str,
            *,
            duplicate_status: bool = False,
            output_plugin: Path | None = None,
            target_state: str | None = None,
        ) -> str:
            trait_values = {
                "localized": localized,
                "light_by_extension": light_by_extension,
                "light_by_header": light_by_header,
                "current_plugin_light": (
                    "true"
                    if "true" in {light_by_extension, light_by_header}
                    else "unknown"
                    if "unknown" in {light_by_extension, light_by_header}
                    else "false"
                ),
                "references_light_master": (
                    "true"
                    if master_name is not None
                    and Path(master_name).suffix.casefold() == ".esl"
                    else "false"
                    if master_name is None
                    or master_name.casefold() == "fallout4.esm"
                    or materialize_master_style_evidence
                    else "unknown"
                ),
                "targets_light_owner": target_state or targets_light_owner,
                "contains_unsupported_light_formids": contains_unsupported_light_formids,
            }
            trait_values["light_context"] = (
                "true"
                if "true"
                in {
                    trait_values["current_plugin_light"],
                    trait_values["targets_light_owner"],
                }
                else "unknown"
                if "unknown"
                in {
                    trait_values["current_plugin_light"],
                    trait_values["targets_light_owner"],
                }
                else "false"
            )
            context = master_context_for(input_plugin, operation)
            context_relative = (
                context.relative_to(self.workspace).as_posix()
                if context is not None
                else "<none>"
            )
            context_sha256 = sha256(context) if context is not None else "<none>"
            lines = [
                "# Mutagen Plugin Text Tool Report",
                "",
                "- game_id: fallout4",
                "- plugin_adapter: mutagen-bethesda-plugin",
                "- plugin_text_capability_level: experimental_write",
                f"- Operation: {operation}",
                f"- Master-style context: {context_relative}",
                f"- Master-style context SHA256: {context_sha256}",
            ]
            lines = [
                line
                for line in lines
                if not (
                    "game_id" in omitted_identity_fields and line.startswith("- game_id:")
                )
                and not (
                    "Operation" in omitted_identity_fields and line.startswith("- Operation:")
                )
            ]
            lines.extend(
                f"- {field}: {value}"
                for field, value in trait_values.items()
                if field not in omitted_traits
            )
            identity_lines = {
                "Input plugin": f"- Input plugin: {input_plugin.relative_to(self.workspace).as_posix()}",
                "Input SHA256": f"- Input SHA256: {sha256(input_plugin)}",
            }
            if status is not None:
                lines.append(f"- Status: {status}")
                if duplicate_status:
                    lines.append(f"- Status: {status}")
            lines.extend(
                value
                for field, value in identity_lines.items()
                if field not in omitted_identity_fields
            )
            if output_plugin is not None:
                lines.extend(
                    [
                        f"- Output plugin: {output_plugin.relative_to(self.workspace).as_posix()}",
                        f"- Output SHA256: {sha256(output_plugin)}",
                    ]
                )
            lines.extend(
                [
                    "- Reason: localized and light words here must not infer traits",
                    "",
                ]
            )
            return "\n".join(lines)

        if legacy_receipt_mode is not None:
            relative_name = plugin_name
            input_plugin = workspace / relative_name
            output_plugin = self.workspace / "out" / "Example" / "tool_outputs" / relative_name
            output_plugin.parent.mkdir(parents=True, exist_ok=True)
            output_plugin.write_bytes(b"legacy-translated-plugin")
            legacy_report = self.workspace / "qa" / f"{Path(relative_name).name}.plugin_stage_mutagen_write.md"
            legacy_report.write_text(
                report_text(
                    "ready",
                    input_plugin,
                    "apply",
                    output_plugin=output_plugin,
                ),
                encoding="utf-8",
            )
            legacy_context = master_context_for(input_plugin, "apply")
            legacy_artifacts = [
                {
                    "path": output_plugin.relative_to(self.workspace).as_posix(),
                    "sha256": sha256(output_plugin),
                },
                {
                    "path": legacy_report.relative_to(self.workspace).as_posix(),
                    "sha256": sha256(legacy_report),
                },
            ]
            legacy_evidence = [legacy_report.relative_to(self.workspace).as_posix()]
            if legacy_context is not None:
                legacy_context_relative = legacy_context.relative_to(self.workspace).as_posix()
                legacy_artifacts.append(
                    {
                        "path": legacy_context_relative,
                        "sha256": sha256(legacy_context),
                    }
                )
                legacy_evidence.append(legacy_context_relative)
            legacy_receipt = self.workspace / "qa" / f"{Path(relative_name).name}.plugin_stage_mutagen_write.adapter_result.json"
            receipt_input = input_plugin
            if legacy_receipt_mode == "invalid":
                receipt_input = self.workspace / "mod" / "unrelated.esp"
                receipt_input.write_bytes(b"unrelated-plugin")
            legacy_receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "adapter_id": "mutagen-bethesda-plugin",
                        "operation": "apply",
                        "status": "success",
                        "mod_name": "Example",
                        "inputs": [
                            {
                                "path": receipt_input.relative_to(self.workspace).as_posix(),
                                "sha256": sha256(receipt_input),
                            }
                        ],
                        "artifacts": legacy_artifacts,
                        "evidence_files": legacy_evidence,
                        "warnings": [],
                        "blockers": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if legacy_receipt_transform is not None:
                legacy_receipt_transform(legacy_receipt)

        def fake_run(_root: Path, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(script)
            if script == "export_esp_strings.py":
                input_plugin = argument(args, "--plugin-path")
                output = argument(args, "--output-path")
                report = argument(args, "--report-path")
                is_output_export = "--allow-generated-plugin" in args
                if is_output_export and "--master-style-manifest" in args:
                    output_manifest = argument(args, "--master-style-manifest")
                    self.assertTrue(output_manifest.is_file())
                    self.assertEqual(
                        json.loads(output_manifest.read_text(encoding="utf-8"))[
                            "schema_version"
                        ],
                        2,
                    )
                current_returncode = (
                    output_export_returncode if is_output_export else export_returncode
                )
                current_status = (
                    output_export_status if is_output_export else export_status
                )
                if current_status is None:
                    current_status = "ready" if current_returncode == 0 else "blocked"
                if (
                    omit_output_export_status
                    if is_output_export
                    else omit_export_status
                ):
                    current_status = None
                duplicate_status = (
                    duplicate_output_export_status
                    if is_output_export
                    else duplicate_export_status
                )
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    report_text(
                        current_status,
                        input_plugin,
                        "export",
                        duplicate_status=duplicate_status,
                        target_state=(
                            "false"
                            if "--master-style-manifest" in args
                            else targets_light_owner
                        ),
                    ),
                    encoding="utf-8",
                )
                if current_returncode == 0:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    rows = [
                        {
                            "schema_version": 2,
                            "game_id": "fallout4",
                            "plugin": plugin_name,
                            "risk": "candidate" if has_candidates else "review",
                            "source": "Visible text",
                            "target": "",
                        }
                    ]
                    if unused_unknown_candidate:
                        rows.append(
                            {
                                "schema_version": 2,
                                "game_id": "fallout4",
                                "plugin": plugin_name,
                                "risk": "candidate",
                                "source": "Unused override",
                                "target": "",
                                "owner_mod_key": master_name,
                                "local_id": 0x800,
                                "master_style": "unknown",
                                "master_style_evidence": (
                                    "unresolved:unseparated-master-order"
                                ),
                            }
                        )
                    elif (
                        targets_light_owner == "unknown"
                        and "--master-style-manifest" not in args
                    ):
                        rows[0].update(
                            {
                                "owner_mod_key": master_name,
                                "local_id": 0x800,
                                "master_style": "unknown",
                                "master_style_evidence": (
                                    "unresolved:unseparated-master-order"
                                ),
                            }
                        )
                    output.write_text(
                        "".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess([], current_returncode, "", "adapter blocked")
            if script == "apply_plugin_translation_map.py":
                export = argument(args, "--export-path")
                output = argument(args, "--output-path")
                output.parent.mkdir(parents=True, exist_ok=True)
                translated_rows = [
                    json.loads(line)
                    for line in export.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                for index, row in enumerate(translated_rows):
                    row["target"] = "translated" if index == 0 else ""
                output.write_text(
                    "".join(json.dumps(row) + "\n" for row in translated_rows),
                    encoding="utf-8",
                )
            elif script == "invoke_mutagen_plugin_text_tool.py":
                input_plugin = argument(args, "--input-plugin-path")
                output = argument(args, "--output-plugin-path")
                report = argument(args, "--report-path")
                report.parent.mkdir(parents=True, exist_ok=True)
                operation = (
                    "verify"
                    if "--mode" in args and args[args.index("--mode") + 1].casefold() == "verify"
                    else "apply"
                )
                current_status = apply_status if operation == "apply" else verify_status
                current_returncode = (
                    apply_returncode if operation == "apply" else verify_returncode
                )
                duplicate_status = (
                    duplicate_apply_status
                    if operation == "apply"
                    else duplicate_verify_status
                )
                if operation == "apply" and current_returncode == 0:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(input_plugin.read_bytes())
                report.write_text(
                    report_text(
                        current_status,
                        input_plugin,
                        operation,
                        duplicate_status=duplicate_status,
                        output_plugin=(
                            output
                            if operation == "apply" and output.is_file()
                            else None
                        ),
                        target_state=(
                            "true"
                            if plugin_resource_evidence.read_plugin_translation_target_light_state(
                                argument(args, "--translation-jsonl-path")
                            )
                            is True
                            else "unknown"
                            if plugin_resource_evidence.read_plugin_translation_target_light_state(
                                argument(args, "--translation-jsonl-path")
                            )
                            is None
                            else "false"
                        ),
                    ),
                    encoding="utf-8",
                )
                if (
                    operation == "apply"
                    and current_returncode == 0
                    and "--adapter-result-path" in args
                ):
                    receipt_path = argument(args, "--adapter-result-path")
                    translation_jsonl = argument(args, "--translation-jsonl-path")
                    context = master_context_for(input_plugin, operation)
                    artifact_paths = [output, report]
                    evidence_paths = [report]
                    if context is not None:
                        artifact_paths.append(context)
                        evidence_paths.append(context)
                    input_paths = [input_plugin, translation_jsonl]
                    if "--master-style-manifest" in args:
                        input_paths.append(argument(args, "--master-style-manifest"))
                    write_adapter_result(
                        receipt_path,
                        build_result(
                            root=self.workspace,
                            status="success",
                            error_code=None,
                            operation="apply",
                            adapter_id="mutagen-bethesda-plugin",
                            artifact_paths=artifact_paths,
                            evidence_paths=evidence_paths,
                            mod_name="Example",
                            input_paths=tuple(input_paths),
                        ),
                    )
                return subprocess.CompletedProcess(
                    [], current_returncode, "", "adapter failed"
                )
            elif script == "verify_plugin_output.py":
                report = argument(args, "--report-output-path")
                original = argument(args, "--original-plugin-path")
                output = argument(args, "--output-plugin-path")
                translation_jsonl = argument(args, "--translation-jsonl-path")
                output_export_jsonl = argument(args, "--output-export-jsonl-path")
                writeback_report = argument(args, "--writeback-report-path")
                invariant_report = argument(args, "--invariant-report-path")
                if not writeback_report.is_absolute():
                    writeback_report = self.workspace / writeback_report
                if not invariant_report.is_absolute():
                    invariant_report = self.workspace / invariant_report
                report.parent.mkdir(parents=True, exist_ok=True)
                metrics = {
                    "Translation rows verified": "1",
                    "Writeback reparse verified": "True",
                    "Structural validation verified": "True",
                    "Round-trip verified": "True",
                    "Verification passed": "True",
                    "Blocking issues": "0",
                }
                metrics.update(post_verify_overrides or {})
                report.write_text(
                    "\n".join(
                        [
                            "# Plugin Output Verification",
                            "",
                            "- game_id: fallout4",
                            "- plugin_adapter: mutagen-bethesda-plugin",
                            f"- Original: {original.relative_to(self.workspace).as_posix()}",
                            f"- Output: {output.relative_to(self.workspace).as_posix()}",
                            f"- Translation JSONL: {translation_jsonl.relative_to(self.workspace).as_posix()}",
                            f"- Output export JSONL: {output_export_jsonl.relative_to(self.workspace).as_posix()}",
                            f"- Writeback report: {writeback_report.relative_to(self.workspace).as_posix()}",
                            f"- Invariant report: {invariant_report.relative_to(self.workspace).as_posix()}",
                            f"- Original SHA256: {sha256(original)}",
                            f"- Output SHA256: {sha256(output)}",
                            f"- Translation JSONL SHA256: {sha256(translation_jsonl)}",
                            f"- Output export JSONL SHA256: {sha256(output_export_jsonl)}",
                            f"- Writeback report SHA256: {sha256(writeback_report)}",
                            f"- Invariant report SHA256: {sha256(invariant_report)}",
                            *[
                                f"- {field}: {value}"
                                for field, value in metrics.items()
                            ],
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        argv = [
            "run_plugin_translation_stage.py",
            "--mod-name",
            "Example",
            "--workspace-path",
            str(workspace),
        ]
        localized_receipt_validation = (
            mock.patch.object(
                plugin_stage,
                "validate_current_localized_receipt",
                return_value=localized_receipt_payload,
            )
            if localized_receipt_valid
            else nullcontext()
        )
        with (
            mock.patch.object(plugin_stage, "project_root", return_value=self.workspace),
            mock.patch.object(plugin_stage, "find_data_root", return_value=workspace),
            mock.patch.object(plugin_stage, "run_python_script", side_effect=fake_run),
            mock.patch.object(sys, "argv", argv),
            localized_receipt_validation,
        ):
            code = plugin_stage.main()
        payload = self.read_json("qa/Example.plugin_translation_stage.json")
        return code, payload, calls

    def stage_used_capabilities(
        self,
        payload: dict[str, object],
    ) -> tuple[Path, dict[str, object]]:
        final_mod = self.workspace / "out" / "Example" / "汉化产出" / "final_mod"
        provenance_rows: list[dict[str, object]] = []
        for row in payload["Plugins"]:
            relative_plugin = Path(row["RelativePath"])
            tool_output = self.workspace / row["ToolOutput"]
            final_plugin = final_mod / relative_plugin
            final_plugin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tool_output, final_plugin)
            provenance_rows.append(
                {
                    "game_id": "fallout4",
                    "file": f"final_mod/{relative_plugin.as_posix()}",
                    "file_sha256": sha256(final_plugin),
                    "source": tool_output.relative_to(self.workspace).as_posix(),
                    "source_sha256": sha256(tool_output),
                    "transform": "controlled-tool-output",
                    "tool": "mutagen-bethesda-plugin",
                    "generated_by": "build_final_mod.py",
                    "status": "assembled",
                    "qa_evidence": [row["ApplyReceipt"]],
                }
            )
        provenance = final_mod / "meta" / "provenance.jsonl"
        provenance.parent.mkdir(parents=True, exist_ok=True)
        provenance.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in provenance_rows),
            encoding="utf-8",
        )
        issues, used_payload = strict_qa.collect_used_capability_gate_issues(
            self.workspace,
            "Example",
            final_mod,
            strict_complete=False,
        )
        self.assertEqual(issues, [])
        return final_mod, used_payload

    def test_strict_qa_binds_stage_artifact_key_receipt(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Nested/Example.esp")
        self.assertEqual(code, 0)
        final_mod, used_payload = self.stage_used_capabilities(payload)

        binding = strict_qa.bind_plugin_write_artifacts(
            self.workspace,
            self.workspace / "work" / "extracted_mods" / "Example",
            "Example",
            Path("Nested/Example.esp"),
            final_mod / "Nested" / "Example.esp",
            used_payload,
            "fallout4",
        )

        self.assertIsNotNone(binding)
        assert binding is not None
        stage_row = payload["Plugins"][0]
        self.assertEqual(
            binding.original.relative_to(
                self.workspace / "work" / "extracted_mods" / "Example"
            ).as_posix(),
            "Nested/Example.esp",
        )
        self.assertEqual(binding.translation, self.workspace / stage_row["TranslationJsonl"])
        self.assertEqual(binding.tool_artifact, self.workspace / stage_row["ToolOutput"])
        self.assertEqual(binding.receipt, self.workspace / stage_row["ApplyReceipt"])
        self.assertEqual(
            binding.apply_report,
            self.workspace
            / next(
                item["report_path"]
                for item in stage_row["CapabilityEvidence"]
                if item.get("phase") == "apply"
            ),
        )

    def test_strict_qa_does_not_cross_bind_nested_same_basename_plugins(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "A/Example.esp",
            additional_plugins=("B/Example.esp",),
        )
        self.assertEqual(code, 0)
        final_mod, used_payload = self.stage_used_capabilities(payload)

        bindings = {
            relative: strict_qa.bind_plugin_write_artifacts(
                self.workspace,
                self.workspace / "work" / "extracted_mods" / "Example",
                "Example",
                Path(relative),
                final_mod / Path(relative),
                used_payload,
                "fallout4",
            )
            for relative in ("A/Example.esp", "B/Example.esp")
        }

        for relative, binding in bindings.items():
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.original.relative_to(
                self.workspace / "work" / "extracted_mods" / "Example"
            ).as_posix(), relative)
            self.assertEqual(
                binding.tool_artifact.relative_to(
                    self.workspace / "out" / "Example" / "tool_outputs"
                ).as_posix(),
                relative,
            )
        self.assertNotEqual(bindings["A/Example.esp"].receipt, bindings["B/Example.esp"].receipt)
        self.assertNotEqual(
            bindings["A/Example.esp"].translation,
            bindings["B/Example.esp"].translation,
        )

    def test_strict_qa_revalidates_receipt_artifact_hashes(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        final_mod, used_payload = self.stage_used_capabilities(payload)
        apply_report = self.workspace / next(
            item["report_path"]
            for item in payload["Plugins"][0]["CapabilityEvidence"]
            if item.get("phase") == "apply"
        )
        apply_report.write_text(
            apply_report.read_text(encoding="utf-8") + "\npost-capability drift\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            strict_qa.bind_plugin_write_artifacts(
                self.workspace,
                self.workspace / "work" / "extracted_mods" / "Example",
                "Example",
                Path("Example.esp"),
                final_mod / "Example.esp",
                used_payload,
                "fallout4",
            )

    def test_strict_qa_rejects_receipt_original_path_rebinding(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Nested/Example.esp")
        self.assertEqual(code, 0)
        final_mod, used_payload = self.stage_used_capabilities(payload)
        wrong_original = (
            self.workspace / "work" / "extracted_mods" / "Example" / "Other" / "Example.esp"
        )
        wrong_original.parent.mkdir(parents=True, exist_ok=True)
        wrong_original.write_bytes(b"different-original")
        receipt = self.workspace / payload["Plugins"][0]["ApplyReceipt"]
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        plugin_input = next(
            item for item in receipt_payload["inputs"] if item["path"].endswith(".esp")
        )
        plugin_input["path"] = wrong_original.relative_to(self.workspace).as_posix()
        plugin_input["sha256"] = sha256(wrong_original)
        receipt.write_text(json.dumps(receipt_payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "does not match final plugin resource path"):
            strict_qa.bind_plugin_write_artifacts(
                self.workspace,
                self.workspace / "work" / "extracted_mods" / "Example",
                "Example",
                Path("Nested/Example.esp"),
                final_mod / "Nested" / "Example.esp",
                used_payload,
                "fallout4",
            )

    def test_strict_qa_rejects_receipt_translation_lane_rebinding(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        final_mod, used_payload = self.stage_used_capabilities(payload)
        wrong_translation = self.workspace / "qa" / "rebound-translation.jsonl"
        wrong_translation.write_text('{"target":"translated"}\n', encoding="utf-8")
        receipt = self.workspace / payload["Plugins"][0]["ApplyReceipt"]
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        translation_input = next(
            item for item in receipt_payload["inputs"] if item["path"].endswith(".jsonl")
        )
        translation_input["path"] = wrong_translation.relative_to(self.workspace).as_posix()
        translation_input["sha256"] = sha256(wrong_translation)
        receipt.write_text(json.dumps(receipt_payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "translation input must stay"):
            strict_qa.bind_plugin_write_artifacts(
                self.workspace,
                self.workspace / "work" / "extracted_mods" / "Example",
                "Example",
                Path("Example.esp"),
                final_mod / "Example.esp",
                used_payload,
                "fallout4",
            )

    def test_same_basename_plugins_use_distinct_stable_artifacts(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "A/Example.esp",
            additional_plugins=("B/Example.esp",),
        )

        self.assertEqual(code, 0)
        plugins = payload["Plugins"]
        self.assertEqual(len(plugins), 2)
        self.assertEqual(
            {row["RelativePath"] for row in plugins},
            {"A/Example.esp", "B/Example.esp"},
        )
        self.assertEqual(len({row["PluginKey"] for row in plugins}), 2)
        self.assertEqual(len({row["TranslationMap"] for row in plugins}), 2)
        self.assertEqual(len({row["TranslationJsonl"] for row in plugins}), 2)
        self.assertEqual(len({row["Evidence"] for row in plugins}), 2)
        for row in plugins:
            self.assertTrue((self.workspace / row["TranslationMap"]).is_file())

    def test_broken_unrelated_master_manifest_is_not_loaded_for_local_target(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            master_name="CustomMaster.esm",
            master_manifest_payload="{broken-json",
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("export_esp_strings.py", calls)
        self.assertIn("build_external_glossary_matches.py", calls)

    def test_broken_light_master_manifest_does_not_block_no_candidate_export(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            master_name="Fallout4.esm",
            master_manifest_payload="{broken-json",
            has_candidates=False,
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "no_candidates")
        self.assertIn("export_esp_strings.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        preflight_reports = list((self.workspace / "qa").glob("*.master-style-preflight.md"))
        self.assertEqual(len(preflight_reports), 1)
        self.assertIn("- Status: not_required", preflight_reports[0].read_text(encoding="utf-8"))

    def test_missing_master_evidence_blocks_only_when_target_owner_is_unknown(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            targets_light_owner="unknown",
            master_name="CustomMaster.esm",
        )

        self.assertEqual(code, 1)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "master_style_preflight_blocked")
        self.assertEqual(calls.count("export_esp_strings.py"), 1)
        self.assertIn("build_external_glossary_matches.py", calls)
        self.assertIn("apply_plugin_translation_map.py", calls)

    def test_unused_unknown_candidate_does_not_trigger_master_preflight(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            targets_light_owner="unknown",
            master_name="CustomMaster.esm",
            unused_unknown_candidate=True,
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("build_external_glossary_matches.py", calls)
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        preflight = next((self.workspace / "qa").glob("*.master-style-preflight.md"))
        self.assertIn("- Status: not_required", preflight.read_text(encoding="utf-8"))

    def test_unknown_owner_collection_ignores_changed_review_rows(self) -> None:
        translation = self.workspace / "translated" / "targets.jsonl"
        translation.parent.mkdir(parents=True, exist_ok=True)
        translation.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "schema_version": 2,
                        "risk": "candidate",
                        "source": "Target",
                        "target": "目标",
                        "owner_mod_key": "TargetMaster.esp",
                        "master_style": "unknown",
                        "master_style_evidence": "unresolved:unseparated-master-order",
                    },
                    {
                        "schema_version": 2,
                        "risk": "review",
                        "source": "Audit",
                        "target": "审计",
                        "owner_mod_key": "UnrelatedMaster.esp",
                        "master_style": "unknown",
                        "master_style_evidence": "unresolved:unseparated-master-order",
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(
            plugin_stage.unresolved_target_master_owners(translation),
            ("TargetMaster.esp",),
        )

    def test_targeted_master_evidence_is_materialized_after_unknown_owner_export(
        self,
    ) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            targets_light_owner="unknown",
            master_name="CustomMaster.esm",
            materialize_master_style_evidence=True,
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertEqual(calls.count("export_esp_strings.py"), 3)
        receipt = self.workspace / plugin["ApplyReceipt"]
        self.assertEqual(
            len(json.loads(receipt.read_text(encoding="utf-8"))["inputs"]),
            3,
        )

    def test_ordinary_plugin_does_not_require_fallout4_master_file(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            master_name="Fallout4.esm",
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        preflight_reports = list((self.workspace / "qa").glob("*.master-style-preflight.md"))
        self.assertEqual(len(preflight_reports), 1)
        self.assertIn("- Status: not_required", preflight_reports[0].read_text(encoding="utf-8"))

    def test_light_plugin_does_not_require_fallout4_master_file(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            master_name="Fallout4.esm",
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        preflight_reports = list((self.workspace / "qa").glob("*.master-style-preflight.md"))
        self.assertEqual(len(preflight_reports), 1)
        self.assertIn("- Status: not_required", preflight_reports[0].read_text(encoding="utf-8"))

    def test_initial_export_report_status_is_strict_and_return_code_bound(self) -> None:
        cases = (
            ("missing", {"omit_export_status": True}),
            ("duplicate", {"duplicate_export_status": True}),
            ("illegal", {"export_status": "complete"}),
            ("blocked_zero", {"export_status": "blocked"}),
            ("error_zero", {"export_status": "error"}),
            ("failed_zero", {"export_status": "failed"}),
            ("ready_nonzero", {"export_status": "ready", "export_returncode": 2}),
        )
        for name, options in cases:
            with self.subTest(name=name):
                self.reset_workspace(f"initial-status-{name}")
                code, payload, calls = self.run_mocked_plugin_stage(
                    "Example.esp",
                    **options,
                )

                self.assertEqual(code, 1)
                plugin = payload["Plugins"][0]
                self.assertEqual(plugin["Status"], "invalid_trait_evidence")
                self.assertNotIn("apply_plugin_translation_map.py", calls)
                attempt = next(
                    row
                    for row in plugin["CapabilityEvidence"]
                    if row.get("phase") == "export"
                )
                self.assertEqual(attempt["result"], "failed")
                self.assertIn("Status", attempt["reason"])

    def test_output_reexport_report_status_is_strict_and_return_code_bound(self) -> None:
        cases = (
            ("missing", {"omit_output_export_status": True}),
            ("duplicate", {"duplicate_output_export_status": True}),
            ("illegal", {"output_export_status": "complete"}),
            ("blocked_zero", {"output_export_status": "blocked"}),
            ("error_zero", {"output_export_status": "error"}),
            ("failed_zero", {"output_export_status": "failed"}),
            (
                "ready_nonzero",
                {"output_export_status": "ready", "output_export_returncode": 2},
            ),
        )
        for name, options in cases:
            with self.subTest(name=name):
                self.reset_workspace(f"output-status-{name}")
                code, payload, calls = self.run_mocked_plugin_stage(
                    "Example.esp",
                    **options,
                )

                self.assertEqual(code, 1)
                plugin = payload["Plugins"][0]
                self.assertEqual(plugin["Status"], "tool_output_export_failed")
                self.assertEqual(calls.count("invoke_mutagen_plugin_text_tool.py"), 1)
                attempt = next(
                    row
                    for row in plugin["CapabilityEvidence"]
                    if row.get("phase") == "output_export"
                )
                self.assertEqual(attempt["result"], "failed")
                self.assertIn("Status", attempt["reason"])

    def test_apply_and_verify_report_statuses_are_strict_and_return_code_bound(self) -> None:
        cases = (
            ("apply_missing", {"apply_status": None}, "apply"),
            ("apply_duplicate", {"duplicate_apply_status": True}, "apply"),
            ("apply_failed_zero", {"apply_status": "failed"}, "apply"),
            ("apply_ready_nonzero", {"apply_returncode": 2}, "apply"),
            ("verify_missing", {"verify_status": None}, "adapter_verify"),
            ("verify_duplicate", {"duplicate_verify_status": True}, "adapter_verify"),
            ("verify_blocked_zero", {"verify_status": "blocked"}, "adapter_verify"),
            ("verify_ready_nonzero", {"verify_returncode": 2}, "adapter_verify"),
        )
        for name, options, phase in cases:
            with self.subTest(name=name):
                self.reset_workspace(f"adapter-status-{name}")
                code, payload, _calls = self.run_mocked_plugin_stage(
                    "Example.esp",
                    **options,
                )

                self.assertEqual(code, 1)
                attempt = next(
                    row
                    for row in payload["Plugins"][0]["CapabilityEvidence"]
                    if row.get("phase") == phase
                )
                self.assertEqual(attempt["result"], "failed")
                self.assertIn("Status", attempt["reason"])

    def test_post_verify_success_metrics_are_validated_before_stage_success(self) -> None:
        for field, value in (
            ("Verification passed", "False"),
            ("Writeback reparse verified", "False"),
            ("Structural validation verified", "False"),
            ("Round-trip verified", "False"),
            ("Blocking issues", "1"),
            ("Translation rows verified", "0"),
        ):
            with self.subTest(field=field):
                self.reset_workspace(f"post-verify-{field.replace(' ', '-').lower()}")
                code, payload, _calls = self.run_mocked_plugin_stage(
                    "Example.esp",
                    post_verify_overrides={field: value},
                )

                self.assertEqual(code, 1)
                plugin = payload["Plugins"][0]
                self.assertEqual(plugin["Status"], "verification_failed")
                attempt = next(
                    row
                    for row in plugin["CapabilityEvidence"]
                    if row.get("phase") == "post_verify"
                )
                self.assertEqual(attempt["result"], "failed")
                self.assertEqual(attempt["error_code"], "invalid_verification_evidence")

    def test_unique_legacy_translation_map_is_atomically_migrated(self) -> None:
        legacy_payload = (
            '{"schema_version": 2, "translations": '
            '[{"source": "Visible text", "target": "translated"}]}\n'
        )
        code, payload, _calls = self.run_mocked_plugin_stage(
            "Nested/Example.esp",
            map_mode="legacy",
            legacy_map_payload=legacy_payload,
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        keyed_map = self.workspace / plugin["TranslationMap"]
        legacy_map = (
            self.workspace
            / "work"
            / "plugin_translation_maps"
            / "Example"
            / "Example.esp.translation_map.json"
        )
        self.assertEqual(keyed_map.read_text(encoding="utf-8"), legacy_payload)
        self.assertFalse(legacy_map.exists())

    def test_legacy_translation_map_with_multiple_hardlinks_is_not_read_or_migrated(self) -> None:
        def add_hardlink(path: Path) -> None:
            shadow = self.workspace / "qa" / "legacy-map-shadow.json"
            os.link(path, shadow)

        code, payload, _calls = self.run_mocked_plugin_stage(
            "Nested/Example.esp",
            map_mode="legacy",
            legacy_map_transform=add_hardlink,
        )

        self.assertEqual(code, 1)
        self.assertTrue(
            any("hardlink" in issue["Message"].casefold() for issue in payload["Issues"]),
            payload["Issues"],
        )

    def test_existing_keyed_map_is_not_overwritten_by_legacy_map(self) -> None:
        keyed_payload = '{"schema_version": 2, "translations": []}\n'
        legacy_payload = (
            '{"schema_version": 2, "translations": '
            '[{"source": "legacy", "target": "must-not-overwrite"}]}\n'
        )
        code, payload, _calls = self.run_mocked_plugin_stage(
            "Example.esp",
            map_mode="both",
            keyed_map_payload=keyed_payload,
            legacy_map_payload=legacy_payload,
        )

        self.assertEqual(code, 0)
        plugin = payload["Plugins"][0]
        keyed_map = self.workspace / plugin["TranslationMap"]
        legacy_map = keyed_map.with_name("Example.esp.translation_map.json")
        self.assertEqual(keyed_map.read_text(encoding="utf-8"), keyed_payload)
        self.assertEqual(legacy_map.read_text(encoding="utf-8"), legacy_payload)

    def test_same_basename_plugins_block_ambiguous_legacy_map_ownership(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "A/Example.esp",
            additional_plugins=("B/Example.esp",),
            map_mode="legacy",
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            {row["Status"] for row in payload["Plugins"]},
            {"blocked_ambiguous_legacy_translation_map"},
        )
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        legacy_map = (
            self.workspace
            / "work"
            / "plugin_translation_maps"
            / "Example"
            / "Example.esp.translation_map.json"
        )
        self.assertTrue(legacy_map.is_file())
        self.assertTrue(
            any("legacy translation map ownership" in issue["Message"].casefold() for issue in payload["Issues"])
        )

    def test_unique_strictly_bound_legacy_receipt_is_cleaned_before_apply(self) -> None:
        code, _payload, _calls = self.run_mocked_plugin_stage(
            "Nested/Example.esp",
            legacy_receipt_mode="valid",
        )

        self.assertEqual(code, 0)
        legacy_receipt = (
            self.workspace
            / "qa"
            / "Example.esp.plugin_stage_mutagen_write.adapter_result.json"
        )
        self.assertFalse(legacy_receipt.exists())

    def test_legacy_receipt_reparse_point_is_not_read_or_claimed(self) -> None:
        def replace_with_symlink(path: Path) -> None:
            target = Path(self.tempdir.name) / "outside-legacy-receipt.json"
            target.write_text('{"outside":true}\n', encoding="utf-8")
            path.unlink()
            try:
                path.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

        code, payload, _calls = self.run_mocked_plugin_stage(
            "Example.esp",
            legacy_receipt_mode="valid",
            legacy_receipt_transform=replace_with_symlink,
        )

        self.assertEqual(code, 1)
        self.assertTrue(
            any(
                "symlink" in issue["Message"].casefold()
                or "reparse" in issue["Message"].casefold()
                for issue in payload["Issues"]
            ),
            payload["Issues"],
        )
        legacy_receipt = (
            self.workspace
            / "qa"
            / "Example.esp.plugin_stage_mutagen_write.adapter_result.json"
        )
        self.assertTrue(legacy_receipt.is_symlink())
        self.assertFalse((self.workspace / "work" / "plugin_receipt_quarantine").exists())

    def test_ambiguous_legacy_receipt_is_quarantined_without_duplicate_claim(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "A/Example.esp",
            additional_plugins=("B/Example.esp",),
            legacy_receipt_mode="valid",
        )

        self.assertEqual(code, 0)
        self.assertFalse(
            (
                self.workspace
                / "qa"
                / "Example.esp.plugin_stage_mutagen_write.adapter_result.json"
            ).exists()
        )
        quarantined = list(
            (self.workspace / "work" / "plugin_receipt_quarantine").rglob("*.unbound.json")
        )
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(
            any("legacy adapter receipt ownership" in issue["Message"].casefold() for issue in payload["Issues"])
        )
        self.assertEqual(len(list((self.workspace / "qa").glob("*.adapter_result.json"))), 2)

    def test_identity_uncertain_legacy_receipt_is_quarantined(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "Example.esp",
            legacy_receipt_mode="invalid",
        )

        self.assertEqual(code, 0)
        quarantined = list(
            (self.workspace / "work" / "plugin_receipt_quarantine").rglob("*.unbound.json")
        )
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(
            any("legacy adapter receipt identity" in issue["Message"].casefold() for issue in payload["Issues"])
        )

    def test_receipt_quarantine_reparse_parent_is_rejected_before_mkdir(self) -> None:
        outside = Path(self.tempdir.name) / "outside-quarantine"
        outside.mkdir()

        def install_quarantine_symlink(_path: Path) -> None:
            quarantine = self.workspace / "work" / "plugin_receipt_quarantine"
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            try:
                quarantine.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "symlink|reparse"):
            self.run_mocked_plugin_stage(
                "Example.esp",
                legacy_receipt_mode="invalid",
                legacy_receipt_transform=install_quarantine_symlink,
            )

        self.assertFalse((outside / "Example").exists())

    def test_failed_export_attempt_is_recorded_with_phase_and_return_code(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "Example.esp",
            export_returncode=2,
            seed_stale_generated=True,
        )

        self.assertEqual(code, 1)
        attempts = [
            row
            for row in payload["Plugins"][0]["CapabilityEvidence"]
            if row.get("evidence_kind") == "adapter_attempt"
        ]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["phase"], "export")
        self.assertEqual(attempts[0]["result"], "blocked")
        self.assertEqual(attempts[0]["return_code"], 2)
        self.assertEqual(attempts[0]["error_code"], "adapter_blocked")
        self.assertTrue(attempts[0]["report_path"].endswith(".md"))
        artifact_key = plugin_stage.plugin_artifact_key("Example", Path("Example.esp"))
        self.assertFalse(
            (self.workspace / "qa" / f"{artifact_key}.apply.adapter_result.json").exists()
        )
        self.assertTrue(
            (
                self.workspace
                / "work"
                / "plugin_translation_maps"
                / "Example"
                / f"{artifact_key}.translation_map.json"
            ).is_file()
        )

    def test_missing_report_identity_field_fails_closed_before_apply(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            omitted_identity_fields=frozenset({"Input SHA256"}),
        )

        self.assertEqual(code, 1)
        plugin = payload["Plugins"][0]
        self.assertEqual(plugin["Status"], "invalid_trait_evidence")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        attempt = next(
            row
            for row in plugin["CapabilityEvidence"]
            if row.get("evidence_kind") == "adapter_attempt"
        )
        self.assertEqual(attempt["phase"], "export")
        self.assertEqual(attempt["result"], "failed")
        self.assertEqual(attempt["error_code"], "invalid_report_identity")

    def test_skyrim_export_report_requires_light_trait_evidence(self) -> None:
        plugin = self.workspace / "work" / "extracted_mods" / "Example" / "Example.esp"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"TES4" + (b"\0" * 20))
        report = self.workspace / "qa" / "skyrim-export.md"
        report.write_text(
            "\n".join(
                [
                    "# Mutagen Plugin Text Tool Report",
                    "",
                    "- game_id: skyrim-se",
                    "- Operation: export",
                    "- Status: ready",
                    "- localized: false",
                    "- light_by_extension: false",
                    "- light_by_header: false",
                    "- current_plugin_light: false",
                    "- references_light_master: false",
                    "- targets_light_owner: false",
                    "- light_context: false",
                    "- contains_unsupported_light_formids: false",
                    "- Master-style context: <none>",
                    "- Master-style context SHA256: <none>",
                    "- Input plugin: work/extracted_mods/Example/Example.esp",
                    f"- Input SHA256: {sha256(plugin)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        skyrim = load_game_profile("skyrim-se")
        resource = plugin_stage.plugin_resource_descriptor(skyrim, Path("Example.esp"))
        status, traits = plugin_stage.read_export_report_evidence(
            skyrim,
            resource,
            report,
            root=self.workspace,
            expected_input=plugin,
            return_code=0,
        )
        self.assertEqual(status, "ready")
        self.assertEqual(traits.resource_traits(), frozenset())

        report.write_text(
            report.read_text(encoding="utf-8").replace(
                "- Status: ready",
                "- Status: blocked",
            ),
            encoding="utf-8",
        )
        blocked, _ = plugin_stage.read_export_report_evidence(
            skyrim,
            resource,
            report,
            root=self.workspace,
            expected_input=plugin,
            return_code=2,
        )
        self.assertEqual(blocked, "blocked")

        fallout4 = load_game_profile("fallout4")
        fallout_resource = plugin_stage.plugin_resource_descriptor(
            fallout4,
            Path("Example.esp"),
        )
        with self.assertRaisesRegex(ValueError, "game_id mismatch"):
            plugin_stage.read_export_report_evidence(
                fallout4,
                fallout_resource,
                report,
                root=self.workspace,
                expected_input=plugin,
                return_code=0,
            )

    def test_readiness_rejects_stale_cross_workspace_and_hash_drift_stage_reports(self) -> None:
        code, baseline, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        report_path = self.workspace / "qa" / "Example.plugin_translation_stage.json"
        plugin_path = self.workspace / "work" / "extracted_mods" / "Example" / "Example.esp"

        cases: list[tuple[str, object]] = [
            ("old_schema", lambda payload: payload.pop("schema")),
            ("cross_workspace", lambda payload: payload.__setitem__("Workspace", "work/extracted_mods/Other")),
            (
                "status_blocking_mismatch",
                lambda payload: payload["Plugins"][0].__setitem__("Status", "writeback_failed"),
            ),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                payload = json.loads(json.dumps(baseline))
                mutate(payload)
                report_path.write_text(json.dumps(payload), encoding="utf-8")
                _path, status, reason = plugin_stage_status(self.workspace, "Example")
                self.assertEqual(status, "invalid")
                self.assertNotEqual(reason, "0")

        report_path.write_text(json.dumps(baseline), encoding="utf-8")
        plugin_path.write_bytes(b"drifted-plugin")
        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual(status, "invalid")
        self.assertIn("hash", reason)

    def test_readiness_accepts_plugin_stage_workspace_at_nested_data_root(self) -> None:
        self.write_marker("fallout4")
        data_root = self.workspace / "work" / "extracted_mods" / "Example" / "Data"
        (data_root / "F4SE").mkdir(parents=True)
        payload = {
            "schema": plugin_stage.PLUGIN_STAGE_SCHEMA,
            "schema_version": plugin_stage.PLUGIN_STAGE_SCHEMA_VERSION,
            "ModName": "Example",
            "game_id": "fallout4",
            "plugin_adapter": "mutagen-bethesda-plugin",
            "Workspace": "work/extracted_mods/Example/Data",
            "ProjectRoot": str(self.workspace),
            "BlockingIssues": 0,
            "Warnings": 0,
            "Plugins": [],
            "Issues": [],
        }
        (self.workspace / "qa" / "Example.plugin_translation_stage.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        _path, status, reason = plugin_stage_status(self.workspace, "Example")

        self.assertEqual((status, reason), ("passed", "0"))

    def test_readiness_does_not_bind_unneeded_master_style_manifest_input(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
            master_name="CustomMaster.esm",
            materialize_master_style_evidence=True,
        )

        self.assertEqual(code, 0)
        receipt = self.workspace / payload["Plugins"][0]["ApplyReceipt"]
        self.assertEqual(
            len(json.loads(receipt.read_text(encoding="utf-8"))["inputs"]),
            2,
        )
        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual((status, reason), ("passed", "0"))

    def test_readiness_rejects_tampered_plugin_capability_evidence(self) -> None:
        def export_attempt(row: dict[str, object]) -> dict[str, object]:
            return next(
                item
                for item in row["CapabilityEvidence"]
                if item.get("phase") == "export"
            )

        def mutate_clear(row: dict[str, object]) -> None:
            row["CapabilityEvidence"] = []

        def mutate_duplicate_export(row: dict[str, object]) -> None:
            row["CapabilityEvidence"].append(
                json.loads(json.dumps(export_attempt(row)))
            )

        def mutate_remove_resolver(row: dict[str, object]) -> None:
            row["CapabilityEvidence"] = [
                item
                for item in row["CapabilityEvidence"]
                if item.get("phase") != "resolve_write"
            ]

        cases = (
            ("clear", mutate_clear),
            ("duplicate_export", mutate_duplicate_export),
            ("remove_resolver", mutate_remove_resolver),
            ("wrong_phase", lambda row: export_attempt(row).__setitem__("phase", "apply")),
            ("failed_result", lambda row: export_attempt(row).__setitem__("result", "failed")),
            ("nonzero_return", lambda row: export_attempt(row).__setitem__("return_code", 1)),
            ("wrong_resource", lambda row: export_attempt(row).__setitem__("resource_path", "Other.esp")),
            ("report_outside_qa", lambda row: export_attempt(row).__setitem__("report_path", row["ToolOutput"])),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self.reset_workspace(f"capability-evidence-{label}")
                code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
                self.assertEqual(code, 0)
                mutate(payload["Plugins"][0])
                report_path = self.workspace / "qa" / "Example.plugin_translation_stage.json"
                report_path.write_text(json.dumps(payload), encoding="utf-8")

                _path, status, reason = plugin_stage_status(self.workspace, "Example")
                self.assertEqual(status, "invalid")
                self.assertNotEqual(reason, "0")

    def test_readiness_rejects_missing_or_hash_drifted_success_evidence(self) -> None:
        def target_path(label: str, row: dict[str, object]) -> Path:
            if label == "translation":
                return self.workspace / row["TranslationJsonl"]
            if label == "tool_output":
                return self.workspace / row["ToolOutput"]
            if label == "verification":
                return self.workspace / row["Evidence"]
            if label == "output_export_jsonl":
                return self.workspace / row["OutputExportJsonl"]
            if label == "receipt":
                return next((self.workspace / "qa").glob("*.apply.adapter_result.json"))
            phase = label.removesuffix("_report")
            attempt = next(
                item
                for item in row["CapabilityEvidence"]
                if item.get("phase")
                in ({"adapter_verify", "verify"} if phase == "adapter_verify" else {phase})
            )
            return self.workspace / attempt["report_path"]

        labels = (
            "translation",
            "tool_output",
            "verification",
            "output_export_jsonl",
            "receipt",
            "export_report",
            "apply_report",
            "output_export_report",
            "adapter_verify_report",
        )
        for mode in ("delete", "drift"):
            for label in labels:
                with self.subTest(mode=mode, label=label):
                    self.reset_workspace(f"success-evidence-{mode}-{label}")
                    code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
                    self.assertEqual(code, 0)
                    target = target_path(label, payload["Plugins"][0])
                    if mode == "delete":
                        target.unlink()
                    else:
                        target.write_bytes(target.read_bytes() + b"\nDRIFT")

                    _path, status, reason = plugin_stage_status(self.workspace, "Example")
                    self.assertEqual(status, "invalid")
                    self.assertNotEqual(reason, "0")

    def test_readiness_rejects_semantically_rebound_apply_receipt(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        row = payload["Plugins"][0]
        receipt = next((self.workspace / "qa").glob("*.apply.adapter_result.json"))
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_payload["adapter_id"] = "different-adapter"
        receipt.write_text(json.dumps(receipt_payload) + "\n", encoding="utf-8")
        row["ApplyReceiptSha256"] = sha256(receipt)
        report_path = self.workspace / "qa" / "Example.plugin_translation_stage.json"
        report_path.write_text(json.dumps(payload), encoding="utf-8")

        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual(status, "invalid")
        self.assertNotEqual(reason, "0")

    def test_readiness_rejects_apply_receipt_bound_to_wrong_report_game(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        row = payload["Plugins"][0]
        apply_attempt = next(
            item
            for item in row["CapabilityEvidence"]
            if item.get("phase") == "apply"
        )
        apply_report = self.workspace / apply_attempt["report_path"]
        apply_report.write_text(
            apply_report.read_text(encoding="utf-8").replace(
                "- game_id: fallout4",
                "- game_id: skyrim-se",
            ),
            encoding="utf-8",
        )
        apply_attempt["report_sha256"] = sha256(apply_report)

        receipt = self.workspace / row["ApplyReceipt"]
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        for artifact in receipt_payload["artifacts"]:
            if artifact["path"] == apply_attempt["report_path"]:
                artifact["sha256"] = sha256(apply_report)
        receipt.write_text(json.dumps(receipt_payload) + "\n", encoding="utf-8")
        row["ApplyReceiptSha256"] = sha256(receipt)
        report_path = self.workspace / "qa" / "Example.plugin_translation_stage.json"
        report_path.write_text(json.dumps(payload), encoding="utf-8")

        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual(status, "invalid")
        self.assertIn("game_id", reason)

    def test_readiness_revalidates_post_verify_semantics_after_report_hash_update(self) -> None:
        def replace_metric(report: Path, field: str, value: str) -> None:
            lines = report.read_text(encoding="utf-8").splitlines()
            prefix = f"- {field}:"
            matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
            self.assertEqual(len(matches), 1)
            lines[matches[0]] = f"- {field}: {value}"
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases = (
            ("verification_false", "Verification passed", "False"),
            ("blocking_nonzero", "Blocking issues", "1"),
            ("wrong_game", "game_id", "skyrim-se"),
            ("wrong_adapter", "plugin_adapter", "different-adapter"),
        )
        for label, field, value in cases:
            with self.subTest(label=label):
                self.reset_workspace(f"post-evidence-{label}")
                code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
                self.assertEqual(code, 0)
                row = payload["Plugins"][0]
                post_attempt = next(
                    item
                    for item in row["CapabilityEvidence"]
                    if item.get("phase") == "post_verify"
                )
                post_report = self.workspace / post_attempt["report_path"]
                replace_metric(post_report, field, value)
                current_hash = sha256(post_report)
                post_attempt["report_sha256"] = current_hash
                row["EvidenceSha256"] = current_hash
                stage_report = self.workspace / "qa" / "Example.plugin_translation_stage.json"
                stage_report.write_text(json.dumps(payload), encoding="utf-8")

                _path, status, reason = plugin_stage_status(self.workspace, "Example")
                self.assertEqual(status, "invalid")
                self.assertNotEqual(reason, "0")

    def test_readiness_rejects_post_verify_artifact_path_substitution(self) -> None:
        code, payload, _calls = self.run_mocked_plugin_stage("Example.esp")
        self.assertEqual(code, 0)
        row = payload["Plugins"][0]
        post_attempt = next(
            item
            for item in row["CapabilityEvidence"]
            if item.get("phase") == "post_verify"
        )
        post_report = self.workspace / post_attempt["report_path"]
        replacement = (
            self.workspace
            / "source"
            / "plugin_exports"
            / "Example"
            / "replacement.tool-output.strings.jsonl"
        )
        replacement.write_text('{"target":"replacement"}\n', encoding="utf-8")
        lines = post_report.read_text(encoding="utf-8").splitlines()
        replacements = {
            "- Output export JSONL:": (
                f"- Output export JSONL: {replacement.relative_to(self.workspace).as_posix()}"
            ),
            "- Output export JSONL SHA256:": (
                f"- Output export JSONL SHA256: {sha256(replacement)}"
            ),
        }
        for prefix, replacement_line in replacements.items():
            matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
            self.assertEqual(len(matches), 1)
            lines[matches[0]] = replacement_line
        post_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        current_hash = sha256(post_report)
        post_attempt["report_sha256"] = current_hash
        row["EvidenceSha256"] = current_hash
        stage_report = self.workspace / "qa" / "Example.plugin_translation_stage.json"
        stage_report.write_text(json.dumps(payload), encoding="utf-8")

        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual(status, "invalid")
        self.assertIn("Output export JSONL", reason)

    def test_no_candidates_readiness_requires_only_export_attempt_chain(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            has_candidates=False,
        )
        self.assertEqual(code, 0)
        row = payload["Plugins"][0]
        self.assertEqual(row["Status"], "no_candidates")
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        attempts = [
            item
            for item in row["CapabilityEvidence"]
            if item.get("evidence_kind") == "adapter_attempt"
        ]
        self.assertEqual([item["phase"] for item in attempts], ["export"])
        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual((status, reason), ("passed", "0"))

        export_report = self.workspace / attempts[0]["report_path"]
        export_report.unlink()
        _path, status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual(status, "invalid")
        self.assertNotEqual(reason, "0")

    def test_workflow_actions_and_tasks_carry_resource_capability_decisions(self) -> None:
        row = {
            "repair_candidates": [
                {
                    "type": "run_command",
                    "command": "python scripts/invoke_mutagen_plugin_text_tool.py --mode Apply",
                    "risk": "low",
                    "reason": "plugin_apply",
                    "allowed": True,
                },
                {
                    "type": "run_command",
                    "command": "python scripts/invoke_bsa_file_extractor_safe.py --mod-name Example",
                    "risk": "low",
                    "reason": "bsa_extract",
                    "allowed": True,
                },
                {
                    "type": "run_command",
                    "command": "python scripts/future_adapter.py",
                    "risk": "low",
                    "reason": "future_capability",
                    "allowed": True,
                    "capability": "future.resource",
                    "operation": "write",
                },
            ]
        }

        actions, blockers = next_actions_from_actions(row, load_game_profile("fallout4"))

        plugin_action, bsa_action, future_action = actions
        self.assertEqual(plugin_action["capability"], "plugin_text")
        self.assertEqual(plugin_action["operation"], "write")
        self.assertEqual(plugin_action["adapter_id"], "mutagen-bethesda-plugin")
        self.assertEqual(plugin_action["capability_level"], "experimental_write")
        self.assertIs(plugin_action["strict_complete_allowed"], False)
        self.assertIs(plugin_action["allowed"], True)
        self.assertEqual(bsa_action["capability"], "archive.bsa")
        self.assertEqual(bsa_action["operation"], "read")
        self.assertEqual(bsa_action["error_code"], "capability_unsupported")
        self.assertIs(bsa_action["allowed"], False)
        self.assertEqual(future_action["capability"], "future.resource")
        self.assertEqual(future_action["error_code"], "capability_unsupported")
        self.assertIs(future_action["allowed"], False)
        self.assertEqual(
            blockers,
            [
                "capability:archive.bsa:read:capability_unsupported",
                "capability:future.resource:write:capability_unsupported",
            ],
        )

        task = task_from_action(
            mod_name="Example",
            state="translated",
            last_success="translated",
            action=plugin_action,
            action_index=0,
            source="repair_candidates",
        )
        for key in (
            "capability",
            "operation",
            "adapter_id",
            "capability_level",
            "strict_complete_allowed",
        ):
            self.assertEqual(task[key], plugin_action[key])

    def test_light_resource_write_action_is_experimental_and_generates_task(self) -> None:
        self.write_marker("fallout4")
        row = {
            "repair_candidates": [
                {
                    "type": "run_command",
                    "command": "python scripts/invoke_mutagen_plugin_text_tool.py --mode Apply",
                    "risk": "low",
                    "reason": "plugin_apply",
                    "allowed": True,
                    "resource_path": "Example.esl",
                    "resource_category": "plugin",
                    "resource_subtype": "plugin",
                    "resource_container": "",
                    "resource_traits": ["light"],
                    "capability": "plugin_text",
                    "operation": "write",
                }
            ]
        }

        actions, blockers = next_actions_from_actions(row, load_game_profile("fallout4"))

        self.assertEqual(len(actions), 1)
        self.assertIs(actions[0]["allowed"], True)
        self.assertEqual(actions[0]["effective_level"], "experimental_write")
        self.assertIs(actions[0]["strict_complete_allowed"], False)
        self.assertEqual(blockers, [])
        context = load_game_profile("fallout4")
        state_path = self.workspace / "qa" / "workflow_state.json"
        state_path.write_text(
            json.dumps(
                {
                    **game_context_metadata(context),
                    "generated_at": "fixture",
                    "states": [
                        {
                            "mod": "Example",
                            "state": "blocked",
                            "last_success_stage": "prepared",
                            "next_actions": actions,
                            "blockers": blockers,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        payload, issues = build_tasks(
            self.workspace,
            state_path,
            self.workspace / "qa" / "missing_previous_tasks.json",
        )
        self.assertFalse([issue for issue in issues if issue.severity == "error"], issues)
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertIs(payload["tasks"][0]["executable"], True)
        self.assertEqual(payload["tasks"][0]["effective_level"], "experimental_write")
        self.assertIs(payload["tasks"][0]["strict_complete_allowed"], False)
        self.assertEqual(payload["counts"]["pending_executable"], 1)
        self.assertEqual(payload["counts"]["pending_manual"], 0)

    def test_fallout4_esl_stage_runs_experimental_write_with_context(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esl",
            light_by_extension="true",
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 0)
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("apply_plugin_translation_map.py", calls)
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        write_evidence = next(
            row
            for row in plugin["CapabilityEvidence"]
            if row["operation"] == "write" and row["phase"] == "resolve_write"
        )
        self.assertEqual(write_evidence["resource_traits"], ["light"])
        self.assertEqual(write_evidence["effective_level"], "experimental_write")
        self.assertIs(write_evidence["supported"], True)
        receipt = self.read_json(plugin["ApplyReceipt"])
        self.assertTrue(
            any(path.startswith("work/plugin_context/") for path in receipt["evidence_files"])
        )

    def test_fallout4_small_flagged_esp_rebuilds_decision_before_apply(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="true",
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 0)
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("apply_plugin_translation_map.py", calls)
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        write_evidence = next(
            row
            for row in plugin["CapabilityEvidence"]
            if row["operation"] == "write" and row["phase"] == "resolve_write"
        )
        self.assertEqual(write_evidence["resource_traits"], ["light"])
        self.assertEqual(write_evidence["effective_level"], "experimental_write")
        self.assertIs(write_evidence["supported"], True)
        receipt = self.read_json(plugin["ApplyReceipt"])
        self.assertTrue(
            any(path.startswith("work/plugin_context/") for path in receipt["evidence_files"])
        )

    def test_fallout4_localized_plugin_routes_to_composite_export_when_receipt_missing(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            localized="true",
            export_returncode=2,
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 1)
        self.assertEqual(plugin["Status"], "localized_delivery_required")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        self.assertIn("invoke_bethesda_localized_delivery.py", calls)
        self.assertTrue(
            any("localized_delivery" in issue["Message"] for issue in payload["Issues"])
        )

    def test_fallout4_localized_plugin_accepts_current_composite_apply_receipt(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            localized="true",
            export_returncode=2,
            localized_receipt_valid=True,
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 0)
        self.assertEqual(plugin["Status"], "localized_delivery_ready")
        self.assertTrue(plugin["Evidence"].endswith("Example.esp.apply.composite.json"))
        self.assertTrue(plugin["EvidenceSha256"])
        self.assertNotIn("invoke_bethesda_localized_delivery.py", calls)
        localized_claim = {
            "operation": "apply",
            "game_id": "fallout4",
            "mod_name": "Example",
            "plugin": {
                "path": "work/extracted_mods/Example/Example.esp",
                "sha256": plugin["InputSha256"],
            },
        }
        with mock.patch.object(
            readiness,
            "validate_composite_receipt",
            return_value=localized_claim,
        ):
            _path, stage_status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual((stage_status, reason), ("passed", "0"))

    def test_localized_receipt_ignores_unrelated_missing_master_style_evidence(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            localized="true",
            light_by_header="true",
            export_returncode=2,
            localized_receipt_valid=True,
            master_name="LightMaster.esp",
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 0)
        self.assertEqual(plugin["Status"], "localized_delivery_ready")
        self.assertNotIn("invoke_bethesda_localized_delivery.py", calls)

    def test_fallout4_unsupported_light_formid_export_blocker_is_preserved(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            contains_unsupported_light_formids="true",
            export_returncode=2,
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 1)
        self.assertEqual(plugin["Status"], "read_only_export_blocked")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        self.assertNotEqual(plugin["Status"], "writeback_failed")

    def test_fallout4_regular_esp_keeps_experimental_apply(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage("Example.esp")

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(plugin["Status"], "experimental_tool_output_ready")
        self.assertIn("apply_plugin_translation_map.py", calls)
        self.assertIn("invoke_mutagen_plugin_text_tool.py", calls)
        self.assertEqual(
            {
                row["phase"]
                for row in plugin["CapabilityEvidence"]
                if row.get("evidence_kind") == "adapter_attempt"
            },
            {"export", "apply", "output_export", "adapter_verify"},
        )
        self.assertEqual(
            [
                row["phase"]
                for row in plugin["CapabilityEvidence"]
                if row.get("evidence_kind") == "verification_attempt"
            ],
            ["post_verify"],
        )
        for field in (
            "TranslationJsonlSha256",
            "ToolOutputSha256",
            "EvidenceSha256",
            "ApplyReceipt",
            "ApplyReceiptSha256",
            "OutputExportJsonl",
            "OutputExportJsonlSha256",
        ):
            self.assertTrue(plugin.get(field), field)
        for attempt in (
            row
            for row in plugin["CapabilityEvidence"]
            if row.get("evidence_kind") in {"adapter_attempt", "verification_attempt"}
        ):
            self.assertTrue(attempt.get("report_sha256"), attempt["phase"])
        _path, readiness_status, reason = plugin_stage_status(self.workspace, "Example")
        self.assertEqual((readiness_status, reason), ("passed", "0"))

    def test_fallout4_plugin_missing_trait_field_fails_closed_before_apply(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            omitted_traits=frozenset({"light_by_header"}),
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 1)
        self.assertEqual(plugin["Status"], "invalid_trait_evidence")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)

    def test_fallout4_unknown_header_trait_blocks_write(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esp",
            light_by_header="unknown",
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 1)
        self.assertEqual(plugin["Status"], "read_only_blocked_for_write")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        write_evidence = next(
            row for row in plugin["CapabilityEvidence"] if row["operation"] == "write"
        )
        self.assertIs(write_evidence["supported"], False)
        self.assertEqual(write_evidence["error_code"], "plugin_trait_unknown")

    def test_fallout4_esl_unknown_non_path_trait_remains_write_blocked(self) -> None:
        code, payload, calls = self.run_mocked_plugin_stage(
            "Example.esl",
            light_by_extension="true",
            contains_unsupported_light_formids="unknown",
        )

        plugin = payload["Plugins"][0]
        self.assertEqual(code, 1)
        self.assertEqual(plugin["Status"], "read_only_blocked_for_write")
        self.assertNotIn("apply_plugin_translation_map.py", calls)
        self.assertNotIn("invoke_mutagen_plugin_text_tool.py", calls)
        write_evidence = next(
            row for row in plugin["CapabilityEvidence"] if row["operation"] == "write"
        )
        self.assertEqual(write_evidence["resource_traits"], ["light"])
        self.assertEqual(write_evidence["error_code"], "plugin_trait_unknown")

    def test_workflow_tasks_do_not_reopen_resolver_blocked_action(self) -> None:
        self.write_marker("fallout4")
        context = load_game_profile("fallout4")
        blocked_write = {
            "type": "run_command",
            "command": "python scripts/future_adapter.py",
            "risk": "low",
            "reason": "future_capability",
            "allowed": True,
            "capability": "future.resource",
            "operation": "write",
        }
        blocked_read_review = {
            "type": "manual_review",
            "command": "",
            "risk": "manual",
            "reason": "review_future_read_capability",
            "allowed": True,
            "capability": "future.resource",
            "operation": "read",
        }
        resolved_actions, _blockers = next_actions_from_actions(
            {"repair_candidates": [blocked_write, blocked_read_review]},
            context,
        )
        state_path = self.workspace / "qa" / "workflow_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    **game_context_metadata(context),
                    "generated_at": "fixture",
                    "states": [
                        {
                            "mod": "Example",
                            "state": "blocked",
                            "last_success_stage": "prepared",
                            "repair_candidates": [blocked_write, blocked_read_review],
                            "next_actions": resolved_actions,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        payload, issues = build_tasks(
            self.workspace,
            state_path,
            self.workspace / "qa" / "missing_previous_tasks.json",
        )

        self.assertFalse([issue for issue in issues if issue.severity == "error"], issues)
        self.assertEqual(len(payload["tasks"]), 1)
        task = payload["tasks"][0]
        self.assertEqual(task["source"], "next_actions")
        self.assertIs(task["executable"], False)
        self.assertEqual(task["status"], "pending_manual")
        self.assertEqual(task["error_code"], "capability_unsupported")
        self.assertEqual(task["capability"], "future.resource")
        self.assertEqual(task["operation"], "read")
        self.assertEqual(payload["counts"]["pending_manual"], 1)

    def build_pex_fixture(self, path: Path) -> Path:
        assert DOTNET is not None
        helper_root = ROOT / ".tmp" / "task-4-pex-fixture-builder"
        helper_root.mkdir(parents=True, exist_ok=True)
        (helper_root / "FixtureBuilder.csproj").write_text(FIXTURE_PROJECT, encoding="utf-8")
        (helper_root / "Program.cs").write_text(textwrap.dedent(FIXTURE_SOURCE), encoding="utf-8")
        helper_dll = helper_root / "bin" / "Debug" / "net8.0" / "FixtureBuilder.dll"
        if not helper_dll.is_file():
            built = subprocess.run(
                [str(DOTNET), "build", str(helper_root / "FixtureBuilder.csproj"), "--nologo"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        generated = subprocess.run(
            [str(DOTNET), str(helper_dll), str(path), "fallout4", "single"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        return path

    def build_plugin_fixture(self, path: Path, name: str) -> Path:
        assert DOTNET is not None
        helper_root = ROOT / ".tmp" / "task-3-plugin-fixture-builder"
        helper_root.mkdir(parents=True, exist_ok=True)
        (helper_root / "FixtureBuilder.csproj").write_text(
            textwrap.dedent(
                """
                <Project Sdk="Microsoft.NET.Sdk">
                  <PropertyGroup>
                    <OutputType>Exe</OutputType>
                    <TargetFramework>net8.0</TargetFramework>
                    <ImplicitUsings>enable</ImplicitUsings>
                  </PropertyGroup>
                  <ItemGroup>
                    <PackageReference Include="Mutagen.Bethesda.Fallout4" Version="0.53.1" />
                  </ItemGroup>
                </Project>
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (helper_root / "Program.cs").write_text(
            textwrap.dedent(
                """
                using Mutagen.Bethesda;
                using Mutagen.Bethesda.Fallout4;
                using Mutagen.Bethesda.Plugins;
                using Mutagen.Bethesda.Plugins.Binary.Parameters;
                using Mutagen.Bethesda.Plugins.Records;

                var output = Path.GetFullPath(args[0]);
                var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(output)), Fallout4Release.Fallout4);
                var weapon = mod.Weapons.AddNew(new FormKey(mod.ModKey, 0x1234));
                weapon.EditorID = "ClassicWeapon";
                weapon.Name = args[1];
                Directory.CreateDirectory(Path.GetDirectoryName(output)!);
                mod.BeginWrite.ToPath(output).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        helper_dll = helper_root / "bin" / "Debug" / "net8.0" / "FixtureBuilder.dll"
        built = subprocess.run(
            [str(DOTNET), "build", str(helper_root / "FixtureBuilder.csproj"), "--nologo"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        generated = self.run_dotnet_adapter(helper_dll, str(path), name)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        return path

    def run_dotnet_adapter(self, adapter_dll: Path, *args: str) -> subprocess.CompletedProcess[str]:
        assert DOTNET is not None
        return subprocess.run(
            [str(DOTNET), str(adapter_dll), *args],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def write_ba2_adapter_config(self) -> None:
        adapter = self.workspace / "tools" / "fake_ba2_adapter.py"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(
            textwrap.dedent(
                """
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--archive-path", required=True)
                parser.add_argument("--output-dir", required=True)
                args = parser.parse_args()
                output = Path(args.output_dir)
                translation = output / "Interface" / "translations" / "Classic_en.txt"
                translation.parent.mkdir(parents=True, exist_ok=True)
                translation.write_text("$HELLO\\tHello", encoding="utf-8")
                material = output / "Materials" / "classic.bgsm"
                material.parent.mkdir(parents=True, exist_ok=True)
                material.write_bytes(b"synthetic-material")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (self.workspace / "config" / "tools.local.json").write_text(
            json.dumps(
                {
                    "DecoderTools": {
                        "Ba2ExtractorPath": "tools/fake_ba2_adapter.py",
                        "Ba2ExtractorProtocol": "skyrim-mod-chs.ba2-extractor.v1",
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_marker_identity_flows_through_state_handoff_and_progress_chain(self) -> None:
        cases = (
            ("skyrim-se", "skyrim-se"),
            ("fallout4", "fallout4"),
        )
        for marker_game, expected_game in cases:
            with self.subTest(marker_game=marker_game):
                for relative in ("qa", ".workflow", "traces"):
                    directory = self.workspace / relative
                    shutil.rmtree(directory)
                    directory.mkdir()
                self.write_marker(marker_game)
                self.run_state_chain()
                for relative in (
                    "qa/translation_readiness.json",
                    "qa/workflow_state.json",
                    "qa/workflow_tasks.json",
                    "qa/codex_handoff.json",
                    "qa/agent_handoff.json",
                    ".workflow/workflow_state.json",
                    ".workflow/progress_card.json",
                ):
                    self.assert_game_metadata(self.read_json(relative), expected_game)
                state = self.read_json("qa/workflow_state.json")
                schema = json.loads((ROOT / "config" / "workflow_state.schema.json").read_text(encoding="utf-8"))
                self.assertTrue(set(schema["required"]).issubset(state), sorted(set(schema["required"]) - set(state)))
                self.assertTrue(set(state).issubset(schema["properties"]), sorted(set(state) - set(schema["properties"])))
                self.assertIn("interface_translation_encoding", schema["required"])
                self.assertNotIn("next_command", schema["properties"])
                self.assertEqual(schema["properties"]["interface_translation_encoding"], {"type": "string"})
                for removed_field in (
                    "plugin_adapter",
                    "pex_category",
                    "pex_writeback_status",
                    "archive_materialization_enabled",
                    "archive_allow_repack",
                ):
                    self.assertNotIn(removed_field, schema["required"])
                    self.assertNotIn(removed_field, schema["properties"])
                state_items = schema["properties"]["states"]["items"]
                self.assertIn("next_actions", state_items["required"])
                next_actions = state_items["properties"]["next_actions"]
                self.assertEqual(next_actions["type"], "array")
                self.assertTrue(
                    {
                        "type",
                        "source",
                        "command",
                        "risk",
                        "reason",
                        "evidence",
                        "allowed",
                        "refresh_after",
                    }.issubset(next_actions["items"]["required"])
                )

        card = (self.workspace / ".workflow" / "progress_card.md").read_text(encoding="utf-8")
        self.assertRegex(card, r"^## \[SMT (?:进度|阻断|完成)\]")
        self.assertIn("Fallout 4 (Experimental)", card)

    def test_state_chain_rejects_marker_without_game_id(self) -> None:
        self.write_marker(None)
        result = self.run_script("audit_translation_readiness.py")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("missing required game_id", result.stdout + result.stderr)

    def test_mismatched_downstream_game_evidence_blocks_state_chain(self) -> None:
        self.write_marker("fallout4")
        stale_report = self.workspace / "qa" / f"{MOD_NAME}.final_binary_review_packet.md"
        stale_report.write_text(
            "\n".join(
                (
                    "# Final Binary Review Packet",
                    "",
                    "- game_id: skyrim-se",
                    "- game_profile_version: 1",
                    "- plugin_adapter: skyrim-mutagen",
                    "- plugin_adapter_version: 1",
                    "- pex_category: Skyrim",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        result = self.run_script("audit_translation_readiness.py", "--mod-name", MOD_NAME)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        readiness = self.read_json("qa/translation_readiness.json")
        self.assertEqual(readiness["OverallStatus"], "blocked")
        issues = readiness.get("Issues", [])
        self.assertTrue(any("game" in str(row).lower() and "mismatch" in str(row).lower() for row in issues), issues)

    def test_fallout4_missing_game_metadata_blocks_state_tasks_and_handoff(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()

        readiness_path = self.workspace / "qa" / "translation_readiness.json"
        readiness = self.read_json("qa/translation_readiness.json")
        damaged_readiness = dict(readiness)
        damaged_readiness.pop("interface_translation_encoding")
        readiness_path.write_text(json.dumps(damaged_readiness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state_result = self.run_script("write_workflow_state.py")
        self.assertNotEqual(state_result.returncode, 0, state_result.stdout + state_result.stderr)
        state = self.read_json("qa/workflow_state.json")
        self.assertTrue(any("missing interface_translation_encoding" in str(row) for row in state["issues"]), state["issues"])

        readiness_path.write_text(json.dumps(readiness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_script("write_workflow_state.py").returncode, 0)
        state_path = self.workspace / "qa" / "workflow_state.json"
        state = self.read_json("qa/workflow_state.json")
        damaged_state = dict(state)
        damaged_state.pop("support_level")
        state_path.write_text(json.dumps(damaged_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tasks_result = self.run_script("write_workflow_tasks.py")
        self.assertNotEqual(tasks_result.returncode, 0, tasks_result.stdout + tasks_result.stderr)
        tasks = self.read_json("qa/workflow_tasks.json")
        self.assertEqual(tasks["tasks"], [])
        self.assertTrue(any("missing support_level" in str(row) for row in tasks["issues"]), tasks["issues"])

        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_script("write_workflow_tasks.py").returncode, 0)
        health = {key: state[key] for key in GAME_KEYS}
        health["Verdict"] = "PASS"
        health_path = self.workspace / "qa" / "workflow_health.json"
        health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifact_paths = {
            "translation_readiness": readiness_path,
            "workflow_state": state_path,
            "workflow_tasks": self.workspace / "qa" / "workflow_tasks.json",
            "workflow_health": health_path,
        }
        for index, (label, path) in enumerate(artifact_paths.items()):
            with self.subTest(label=label):
                original = json.loads(path.read_text(encoding="utf-8-sig"))
                missing_key = sorted(GAME_KEYS)[index]
                damaged = dict(original)
                damaged.pop(missing_key)
                path.write_text(json.dumps(damaged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = self.run_script("write_codex_handoff.py")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                handoff = self.read_json("qa/codex_handoff.json")
                self.assertTrue(
                    any(label in str(row) and f"missing {missing_key}" in str(row) for row in handoff["issues"]),
                    handoff["issues"],
                )
                path.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_progress_from_state_rejects_marker_mismatch_without_overwriting_completion(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()
        state_path = self.workspace / "qa" / "workflow_state.json"
        state = self.read_json("qa/workflow_state.json")
        state.update(
            {
                "game_id": "skyrim-se",
                "game_display_name": "Skyrim Special Edition",
                "support_level": "stable",
            }
        )
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        progress_state = self.workspace / ".workflow" / "workflow_state.json"
        progress_card = self.workspace / ".workflow" / "progress_card.json"
        before_state = progress_state.read_bytes()
        before_card = progress_card.read_bytes()

        result = self.run_script("workflow_progress.py", "from-state")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(progress_state.read_bytes(), before_state)
        self.assertEqual(progress_card.read_bytes(), before_card)

    def test_agent_handoff_freshness_rejects_marker_change_without_rewrite(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()
        handoff_path = self.workspace / "qa" / "agent_handoff.json"
        before = handoff_path.read_bytes()
        self.write_marker("skyrim-se")

        result = self.run_script("write_agent_handoff.py", "--check-freshness")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        freshness = json.loads(result.stdout)
        self.assertFalse(freshness["fresh"])
        self.assertTrue(any(row["reason"] == "game_metadata_mismatch" for row in freshness["reasons"]))
        self.assertEqual(handoff_path.read_bytes(), before)

    def test_classic_name_route_uses_marker_not_filename_guessing(self) -> None:
        mod_root = self.workspace / "mod" / MOD_NAME
        dll = mod_root / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        material = mod_root / "Materials" / "classic.bgsm"
        dll.parent.mkdir(parents=True)
        material.parent.mkdir(parents=True)
        dll.write_bytes(b"same-synthetic-dll")
        material.write_bytes(b"same-synthetic-material")
        routes: dict[str, dict[str, dict[str, object]]] = {}
        inventories: dict[str, str] = {}
        for game_id in ("fallout4", "skyrim-se"):
            with self.subTest(game_id=game_id):
                self.write_marker(game_id)
                routes[game_id] = {}
                for label, path in (("dll", dll), ("material", material)):
                    result = self.run_script(
                        "route_translation_task.py",
                        "--file-path",
                        str(path.relative_to(self.workspace)),
                        "--as-json",
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    routes[game_id][label] = json.loads(result.stdout)
                inventory = self.run_script(
                    "detect_mod_files.py",
                    "--scan-path",
                    f"mod/{MOD_NAME}",
                    "--report-path",
                    f"qa/{game_id}.inventory.md",
                )
                self.assertEqual(inventory.returncode, 0, inventory.stdout + inventory.stderr)
                inventories[game_id] = (self.workspace / "qa" / f"{game_id}.inventory.md").read_text(encoding="utf-8")

        self.assertEqual(routes["fallout4"]["material"]["risk"], "Profile-protected resource")
        self.assertEqual(routes["fallout4"]["material"]["primary_tool"], "Copy unchanged")
        self.assertIn("fallout4", str(routes["fallout4"]["material"]["notes"]).lower())
        self.assertIn("materials", str(routes["fallout4"]["material"]["notes"]).lower())
        self.assertEqual(routes["skyrim-se"]["material"]["risk"], "Unknown")
        self.assertNotEqual(routes["fallout4"]["material"]["notes"], routes["skyrim-se"]["material"]["notes"])
        self.assertIn("recognized data directory 'f4se'", str(routes["fallout4"]["dll"]["notes"]).lower())
        self.assertIn("not a recognized data directory", str(routes["skyrim-se"]["dll"]["notes"]).lower())
        self.assertIn("- GameId: fallout4", inventories["fallout4"])
        self.assertIn("Profile-protected resource", inventories["fallout4"])
        self.assertIn("- GameId: skyrim-se", inventories["skyrim-se"])
        self.assertNotIn("Profile-protected resource", inventories["skyrim-se"])

    def test_fallout4_final_mod_uses_profile_and_binds_metadata_and_hashes(self) -> None:
        self.write_marker("fallout4")
        source = self.workspace / "mod" / MOD_NAME / "Data"
        dll = source / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"synthetic-f4se-dll-not-a-real-mod-binary\x00\x01")
        (source / "Materials").mkdir()
        (source / "Materials" / "classic.bgsm").write_bytes(b"synthetic-material")
        (source / "MCM" / "Config" / "ClassicHolsteredWeapons").mkdir(parents=True)
        (source / "MCM" / "Config" / "ClassicHolsteredWeapons" / "config.json").write_text(
            '{"modName":"Classic Holstered Weapons"}\n', encoding="utf-8"
        )
        self.write_dictionary()
        attempted_dll_overlay = (
            self.workspace
            / "translated"
            / "tool_outputs"
            / MOD_NAME
            / "F4SE"
            / "Plugins"
            / "ClassicHolsteredWeapons.dll"
        )
        attempted_dll_overlay.parent.mkdir(parents=True)
        attempted_dll_overlay.write_bytes(b"attempted-modified-dll")

        original_hash = sha256(dll)
        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            MOD_NAME,
            "--source-mod-dir",
            f"mod/{MOD_NAME}",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        final_dll = final_mod / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        self.assertTrue(final_dll.is_file())
        self.assertEqual(sha256(final_dll), original_hash)
        self.assertFalse((final_mod / "Data").exists())

        manifest = self.read_json(f"out/{MOD_NAME}/汉化产出/final_mod/meta/manifest.json")
        self.assert_game_metadata(manifest, "fallout4")
        provenance = [
            json.loads(line)
            for line in (final_mod / "meta" / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(provenance)
        self.assertTrue(all(GAME_KEYS.issubset(row) for row in provenance))

        validated = self.run_script("validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        report = (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
        self.assertIn("Fallout 4 (Experimental)", report)
        self.assertNotIn("No common Skyrim Data directories", report)

        manifest_path = final_mod / "meta" / "manifest.json"
        for key, value in (
            ("game_id", "skyrim-se"),
            ("game_profile_version", 999),
            ("game_display_name", "Skyrim Special Edition"),
            ("support_level", "stable"),
            ("interface_translation_encoding", "utf-8"),
        ):
            with self.subTest(manifest_metadata=key):
                tampered_manifest = {**manifest, key: value}
                manifest_path.write_text(
                    json.dumps(tampered_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                tampered = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod"
                )
                self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)
                self.assertIn("Manifest game metadata mismatch", (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        provenance_path = final_mod / "meta" / "provenance.jsonl"
        original_provenance = provenance_path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in original_provenance.splitlines() if line.strip()]
        rows[0]["support_level"] = "stable"
        provenance_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
        tampered = self.run_script("validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod")
        self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)
        self.assertIn("Provenance game metadata mismatch", (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
        provenance_path.write_text(original_provenance, encoding="utf-8")

    def test_zip_sources_bind_member_hash_for_skyrim_and_fallout4(self) -> None:
        cases = (
            ("skyrim-se", "Meshes/protected.nif", "SKSE/Plugins/Example.dll"),
            ("fallout4", "Materials/protected.bgsm", "F4SE/Plugins/Example.dll"),
        )
        for game_id, protected_entry, dll_entry in cases:
            with self.subTest(game_id=game_id):
                self.reset_workspace(f"zip-{game_id}")
                self.write_marker(game_id)
                archive_path = self.workspace / "mod" / f"{game_id}.zip"
                protected_bytes = f"{game_id}-protected".encode()
                dll_bytes = f"{game_id}-dll".encode()
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(protected_entry, protected_bytes)
                    archive.writestr(dll_entry, dll_bytes)
                self.assertEqual(
                    final_source_hash(self.workspace, f"mod/{game_id}.zip::{protected_entry}"),
                    hashlib.sha256(protected_bytes).hexdigest(),
                )
                self.write_dictionary("ZipMod")

                built = self.run_script(
                    "build_final_mod.py",
                    "--mod-name",
                    "ZipMod",
                    "--source-mod-dir",
                    f"mod/{game_id}.zip",
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = self.workspace / "out" / "ZipMod" / "汉化产出" / "final_mod"
                validated = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                )
                self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
                rows = [
                    json.loads(line)
                    for line in (final_mod / "meta" / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                by_file = {row["file"]: row for row in rows}
                for entry, content in ((protected_entry, protected_bytes), (dll_entry, dll_bytes)):
                    row = by_file[f"final_mod/{entry}"]
                    self.assertEqual(row["source_sha256"], hashlib.sha256(content).hexdigest())
                    self.assertEqual(row["source_archive"], f"mod/{game_id}.zip")
                    self.assertEqual(row["source_archive_entry"], entry)
                    self.assertEqual(row["source_archive_sha256"], sha256(archive_path))
                provenance_path = final_mod / "meta" / "provenance.jsonl"
                original_provenance = provenance_path.read_text(encoding="utf-8")
                for mutation, field, replacement in (
                    ("missing-path", "source_archive", None),
                    ("missing-entry", "source_archive_entry", None),
                    ("missing-hash", "source_archive_sha256", None),
                    ("wrong-path", "source_archive", "mod/wrong.zip"),
                    ("wrong-entry", "source_archive_entry", "wrong/entry.bin"),
                    ("wrong-hash", "source_archive_sha256", "0" * 64),
                ):
                    with self.subTest(game_id=game_id, provenance_mutation=mutation):
                        tampered_rows = [json.loads(line) for line in original_provenance.splitlines() if line]
                        target = next(row for row in tampered_rows if row["file"] == f"final_mod/{protected_entry}")
                        if replacement is None:
                            target.pop(field)
                        else:
                            target[field] = replacement
                        provenance_path.write_text(
                            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tampered_rows),
                            encoding="utf-8",
                        )
                        rejected = self.run_script(
                            "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                        )
                        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                        self.assertIn(field, (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
                        provenance_path.write_text(original_provenance, encoding="utf-8")
                delivered_dll = final_mod / Path(dll_entry)
                delivered_dll.write_bytes(b"tampered")
                tampered = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                )
                self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)

    def test_zip_source_cannot_bypass_localized_composite_delivery(self) -> None:
        self.write_marker("fallout4")
        archive_path = self.workspace / "mod" / "LocalizedZip.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("LocalizedZip.esp", b"TES4" + (b"\x00" * 20))
            archive.writestr("Strings/LocalizedZip_english.strings", b"source-table")
        output = (
            self.workspace
            / "translated"
            / "tool_outputs"
            / "LocalizedZip"
            / "Strings"
            / "LocalizedZip_chinese.strings"
        )
        output.parent.mkdir(parents=True)
        output.write_bytes(b"translated-table")
        self.write_dictionary("LocalizedZip")

        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            "LocalizedZip",
            "--source-mod-dir",
            "mod/LocalizedZip.zip",
            "--force",
        )

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertIn("requires a materialized source directory", built.stdout)
        self.assertFalse(
            (
                self.workspace
                / "out"
                / "LocalizedZip"
                / "汉化产出"
                / "final_mod"
                / "Strings"
                / "LocalizedZip_chinese.strings"
            ).exists()
        )

    def test_generated_meta_paths_are_reserved_for_directory_and_zip_rebuilds(self) -> None:
        for source_kind in ("directory", "zip"):
            with self.subTest(source_kind=source_kind):
                self.reset_workspace(f"reserved-meta-{source_kind}")
                self.write_marker("fallout4")
                sentinel_files = {
                    "meta/provenance.jsonl": "input-provenance-sentinel\n",
                    "meta/manifest.json": '{"input":"manifest-sentinel"}\n',
                    "meta/redistribution_notes.md": "input-redistribution-sentinel\n",
                    "meta/build_report.md": "input-build-report-sentinel\n",
                }
                if source_kind == "directory":
                    source = self.workspace / "mod" / "ReservedMetaMod"
                    for relative, content in sentinel_files.items():
                        path = source / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                    (source / "Interface").mkdir(exist_ok=True)
                    (source / "Interface" / "payload.txt").write_text("payload", encoding="utf-8")
                    source_arg = "mod/ReservedMetaMod"
                else:
                    source = self.workspace / "mod" / "ReservedMetaMod.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        for relative, content in sentinel_files.items():
                            archive.writestr(relative, content)
                        archive.writestr("Interface/payload.txt", "payload")
                    source_arg = "mod/ReservedMetaMod.zip"
                self.write_dictionary("ReservedMetaMod")

                for _attempt in range(2):
                    built = self.run_script(
                        "build_final_mod.py",
                        "--mod-name",
                        "ReservedMetaMod",
                        "--source-mod-dir",
                        source_arg,
                        "--force",
                    )
                    self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                    validated = self.run_script(
                        "validate_final_mod.py", "--final-mod-dir", "out/ReservedMetaMod/汉化产出/final_mod"
                    )
                    self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

                final_meta = self.workspace / "out" / "ReservedMetaMod" / "汉化产出" / "final_mod" / "meta"
                for relative, sentinel in sentinel_files.items():
                    self.assertNotEqual((final_meta.parent / relative).read_text(encoding="utf-8"), sentinel)
                rows = [
                    json.loads(line)
                    for line in (final_meta / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
                    if line
                ]
                files = [row["file"].lower() for row in rows]
                self.assertEqual(files.count("final_mod/meta/provenance.jsonl"), 1)
                self.assertEqual(len(files), len(set(files)))
                for reserved in sentinel_files:
                    row = next(item for item in rows if item["file"].lower() == f"final_mod/{reserved}".lower())
                    self.assertTrue(
                        row["source"].startswith("generated:"),
                        f"reserved path reused input provenance: {row}",
                    )

    def test_strict_qa_summary_carries_fallout4_identity(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        (workspace / "F4SE").mkdir(parents=True)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        final_mod.mkdir(parents=True)
        result = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(result.returncode, 0)
        report = self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md"
        self.assertTrue(report.is_file(), result.stdout + result.stderr)
        text = report.read_text(encoding="utf-8")
        self.assertIn("- game_id: fallout4", text)
        self.assertIn("- interface_translation_encoding: utf-16-le-bom", text)
        self.assertNotIn("- pex_writeback_status:", text)

    @unittest.skipIf(WORKSPACE_SAFE_DOTNET is None, "a project-local .NET 8 SDK is required")
    def test_strict_chain_blocks_experimental_capabilities_used_by_final_delivery(self) -> None:
        self.write_marker("fallout4")
        (self.workspace / "config" / "tools.local.json").write_text(
            json.dumps({"DecoderTools": {"DotNetSdkPath": str(WORKSPACE_SAFE_DOTNET)}}) + "\n",
            encoding="utf-8",
        )
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        workspace.mkdir(parents=True)
        final_mod.mkdir(parents=True)

        plugin_name = "ClassicHolsteredWeapons.esp"
        original_plugin = self.build_plugin_fixture(workspace / plugin_name, "Classic Weapon")
        original_export = self.workspace / "source" / "plugin_exports" / MOD_NAME / f"{plugin_name}_strings.jsonl"
        exported_plugin = self.run_script(
            "export_esp_strings.py",
            "--plugin-path",
            str(original_plugin),
            "--mod-name",
            MOD_NAME,
            "--output-path",
            str(original_export),
            "--report-path",
            f"qa/{plugin_name}.integration_source_export.md",
            "--game",
            "fallout4",
        )
        self.assertEqual(exported_plugin.returncode, 0, exported_plugin.stdout + exported_plugin.stderr)
        source_rows = [json.loads(line) for line in original_export.read_text(encoding="utf-8").splitlines() if line]
        source_row = next(row for row in source_rows if row.get("record_type") == "WEAP" and row.get("subrecord_type") == "FULL")
        plugin_translation = self.workspace / "translated" / "plugin_exports" / MOD_NAME / f"{plugin_name}_strings.zh.jsonl"
        plugin_translation.parent.mkdir(parents=True)
        plugin_translation.write_text(
            json.dumps(
                {
                    **source_row,
                    "target": "经典武器",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        tool_plugin = self.workspace / "out" / MOD_NAME / "tool_outputs" / plugin_name
        plugin_writeback_report = self.workspace / "qa" / f"{plugin_name}.plugin_stage_mutagen_write.md"
        plugin_apply_receipt = self.workspace / "qa" / f"{plugin_name}.plugin_apply.adapter_result.json"
        applied = self.run_script(
            "invoke_mutagen_plugin_text_tool.py",
            "--mode",
            "Apply",
            "--input-plugin-path",
            str(workspace / plugin_name),
            "--translation-jsonl-path",
            str(plugin_translation),
            "--output-plugin-path",
            str(tool_plugin),
            "--report-path",
            str(plugin_writeback_report),
            "--adapter-result-path",
            str(plugin_apply_receipt),
            "--game",
            "fallout4",
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(
            validate_plugin_report_status(
                plugin_writeback_report,
                return_code=applied.returncode,
            ),
            "ready",
        )
        shutil.copy2(tool_plugin, final_mod / plugin_name)

        original_pex = self.build_pex_fixture(workspace / "Scripts" / "ClassicHolsteredWeapons.pex")
        exported_pex = self.workspace / "source" / "pex_exports" / MOD_NAME / "ClassicHolsteredWeapons.pex_strings.jsonl"
        exported_pex.parent.mkdir(parents=True)
        pex_export_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_export.md"
        exported = self.run_script(
            "invoke_mutagen_pex_string_tool.py",
            "--mode",
            "Export",
            "--input-pex-path",
            str(original_pex),
            "--output-jsonl-path",
            str(exported_pex),
            "--report-path",
            str(pex_export_report),
            "--game",
            "fallout4",
        )
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        pex_translation = self.workspace / "work" / "normalized" / MOD_NAME / "pex_apply" / "ClassicHolsteredWeapons.translation.jsonl"
        pex_translation.parent.mkdir(parents=True)
        export_rows = [json.loads(line) for line in exported_pex.read_text(encoding="utf-8").splitlines() if line]
        translated_rows = [
            {
                **row,
                "Result": "共享可见文本",
                "risk": "candidate",
                "notes": "confirmed visible text for synthetic integration fixture",
            }
            for row in export_rows
            if row.get("Source") == "Shared visible text"
        ]
        self.assertTrue(translated_rows, export_rows)
        pex_translation.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in translated_rows), encoding="utf-8"
        )
        tool_pex = self.workspace / "out" / MOD_NAME / "tool_outputs" / "Scripts" / original_pex.name
        tool_pex.parent.mkdir(parents=True)
        pex_apply_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_apply.md"
        pex_apply_receipt = self.workspace / "qa" / "ClassicHolsteredWeapons.pex_apply.adapter_result.json"
        pex_applied = self.run_script(
            "invoke_mutagen_pex_string_tool.py",
            "--mode",
            "Apply",
            "--input-pex-path",
            str(original_pex),
            "--translation-jsonl-path",
            str(pex_translation),
            "--output-pex-path",
            str(tool_pex),
            "--report-path",
            str(pex_apply_report),
            "--adapter-result-path",
            str(pex_apply_receipt),
            "--game",
            "fallout4",
            "--allow-experimental-writeback",
        )
        self.assertEqual(pex_applied.returncode, 0, pex_applied.stdout + pex_applied.stderr)
        pex_verify_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_verify.md"
        pex_verified = self.run_script(
            "invoke_mutagen_pex_string_tool.py",
            "--mode",
            "Verify",
            "--input-pex-path",
            str(original_pex),
            "--translation-jsonl-path",
            str(pex_translation),
            "--output-pex-path",
            str(tool_pex),
            "--report-path",
            str(pex_verify_report),
            "--apply-adapter-result-path",
            str(pex_apply_receipt),
            "--game",
            "fallout4",
        )
        self.assertEqual(pex_verified.returncode, 0, pex_verified.stdout + pex_verified.stderr)
        final_pex = final_mod / "Scripts" / original_pex.name
        final_pex.parent.mkdir(parents=True)
        shutil.copy2(tool_pex, final_pex)
        provenance = final_mod / "meta" / "provenance.jsonl"
        provenance.parent.mkdir(parents=True)
        provenance_rows = [
            {
                "game_id": "fallout4",
                "file": f"final_mod/{plugin_name}",
                "file_sha256": sha256(final_mod / plugin_name),
                "source": tool_plugin.relative_to(self.workspace).as_posix(),
                "source_sha256": sha256(tool_plugin),
                "transform": "controlled-tool-output",
                "tool": "mutagen-bethesda-plugin",
                "generated_by": "build_final_mod.py",
                "status": "assembled",
                "qa_evidence": [plugin_apply_receipt.relative_to(self.workspace).as_posix()],
            },
            {
                "game_id": "fallout4",
                "file": f"final_mod/Scripts/{original_pex.name}",
                "file_sha256": sha256(final_pex),
                "source": tool_pex.relative_to(self.workspace).as_posix(),
                "source_sha256": sha256(tool_pex),
                "transform": "controlled-tool-output",
                "tool": "mutagen-pex",
                "generated_by": "build_final_mod.py",
                "status": "assembled",
                "qa_evidence": [pex_apply_receipt.relative_to(self.workspace).as_posix()],
            },
        ]
        provenance.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in provenance_rows),
            encoding="utf-8",
        )

        result = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(result.returncode, 0)
        strict_report = (self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md").read_text(encoding="utf-8")
        plugin_gate_key = plugin_stage.plugin_artifact_key(MOD_NAME, Path(plugin_name))
        plugin_report = self.workspace / "qa" / f"{plugin_gate_key}.gate-plugin-output-verification.md"
        plugin_invariant_report = self.workspace / "qa" / f"{plugin_gate_key}.gate-plugin-binary-invariant.md"
        plugin_gate_export_report = self.workspace / "qa" / f"{plugin_gate_key}.gate-final-mod-export.md"
        plugin_output_export = self.workspace / "source" / "plugin_exports" / MOD_NAME / f"{plugin_gate_key}.gate-final-mod.strings.jsonl"
        pex_report = self.workspace / "qa" / f"{MOD_NAME}.pex_delivery_post_build.md"
        used_capabilities = self.workspace / "qa" / f"{MOD_NAME}.used_capabilities.json"
        export_failure = plugin_gate_export_report.read_text(encoding="utf-8") if plugin_gate_export_report.is_file() else ""
        invariant_failure = plugin_invariant_report.read_text(encoding="utf-8") if plugin_invariant_report.is_file() else ""
        self.assertTrue(
            plugin_report.is_file(),
            strict_report + "\n" + export_failure + "\n" + invariant_failure + "\n" + result.stdout + result.stderr,
        )
        self.assertIn("No blocking issues.", plugin_report.read_text(encoding="utf-8"))
        self.assertTrue(plugin_invariant_report.is_file(), result.stdout + result.stderr)
        self.assertEqual(
            validate_plugin_report_status(plugin_invariant_report, return_code=0),
            "ready",
        )
        self.assertEqual(
            validate_plugin_report_status(plugin_gate_export_report, return_code=0),
            "ready",
        )
        self.assertIn("Operation: verify", plugin_invariant_report.read_text(encoding="utf-8"))
        self.assertIn("Parsed structural and payload invariant verified: True", plugin_invariant_report.read_text(encoding="utf-8"))
        self.assertTrue(plugin_output_export.is_file(), result.stdout + result.stderr)
        self.assertIn("Parsed structural and payload invariant verified: True", plugin_writeback_report.read_text(encoding="utf-8"))
        self.assertTrue(pex_report.is_file())
        self.assertIn("- Blocking issues: 0", pex_report.read_text(encoding="utf-8"))
        self.assertNotIn("missing", pex_report.read_text(encoding="utf-8").lower())
        self.assertIn("- experimental_opt_in: True", pex_apply_report.read_text(encoding="utf-8"))
        self.assertIn("- Verification passed: True", pex_verify_report.read_text(encoding="utf-8"))
        self.assertTrue(used_capabilities.is_file(), strict_report)
        used_payload = json.loads(used_capabilities.read_text(encoding="utf-8"))
        self.assertEqual(
            {(row["name"], row["level"]) for row in used_payload["capabilities"]},
            {("plugin_text", "experimental_write"), ("pex", "experimental_write")},
        )
        self.assertIn("- Final plugins checked: 1", strict_report)
        self.assertIn("- Final PEX files checked: 1", strict_report)
        self.assertNotIn("| error | plugin-output |", strict_report)
        self.assertNotIn("| error | pex-delivery |", strict_report)
        self.assertEqual(strict_report.count("| error | used-capability-experimental-restriction |"), 2)
        self.assertNotIn("pex-experimental-gate", strict_report)
        self.assertNotIn("tool output PEX is missing", strict_report)

        plugin_writeback_report.unlink()
        missing_evidence = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(missing_evidence.returncode, 0)
        missing_report = (self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md").read_text(encoding="utf-8")
        self.assertIn("| error | used-capability-verification-failed |", missing_report)
        self.assertIn("| error | plugin-output |", missing_report)
        self.assertIn("artifact binding", missing_report.lower())

    def test_ba2_verified_safe_production_evidence_is_accepted_by_strict_coverage(self) -> None:
        self.write_marker("fallout4")
        self.write_ba2_adapter_config()
        archive = self.workspace / "mod" / "ClassicHolsteredWeapons - Main.ba2"
        write_test_ba2(archive)
        extracted = self.run_script(
            "invoke_ba2_extractor_safe.py",
            "--mod-name",
            MOD_NAME,
            "--archive-path",
            "mod/ClassicHolsteredWeapons - Main.ba2",
            "--output-dir",
            f"work/archive_extracts/{MOD_NAME}/ClassicHolsteredWeapons - Main",
            "--config-path",
            "config/tools.local.json",
        )
        self.assertEqual(extracted.returncode, 0, extracted.stdout + extracted.stderr)
        manifest_path = (
            self.workspace
            / "out"
            / MOD_NAME
            / "archive_audits"
            / "ClassicHolsteredWeapons - Main"
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["game_id"], "fallout4")
        self.assertEqual(manifest["AuditMode"], "verified-safe-extraction")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        workspace.mkdir(parents=True)
        shutil.copy2(archive, workspace / archive.name)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        for row in manifest["Files"]:
            destination = final_mod / Path(row["RelativePath"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = self.workspace / row["ProjectPath"]
            shutil.copy2(source, destination)

        audited = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
        payload = json.loads(audited.stdout)
        evidence = next(row for row in payload["Archives"] if row["Extension"] == ".ba2")
        self.assertTrue(evidence["EvidenceValid"])
        self.assertTrue(evidence["MaterializationReady"])
        self.assertEqual(evidence["EvidenceMode"], "verified-safe-extraction")
        self.assertEqual(payload["BlockingIssues"], 0)

    def test_string_tables_without_delivery_evidence_fail_strict_chain(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                self.write_marker(game_id)
                workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
                strings = workspace / "Strings" / f"{game_id}_english.strings"
                strings.parent.mkdir(parents=True, exist_ok=True)
                strings.write_bytes(b"synthetic-localized-string-table")
                final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
                final_mod.mkdir(parents=True, exist_ok=True)

                result = self.run_script(
                    "run_non_gui_qa_gates.py",
                    "--mod-name",
                    MOD_NAME,
                    "--workspace-path",
                    f"work/extracted_mods/{MOD_NAME}",
                    "--final-mod-dir",
                    f"out/{MOD_NAME}/汉化产出/final_mod",
                    "--strict-complete",
                )
                self.assertNotEqual(result.returncode, 0)
                report = self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md"
                text = report.read_text(encoding="utf-8")
                self.assertIn("Localized string tables:", text)
                self.assertIn("used-capability-verification-failed", text)
                self.assertNotIn("STRINGS delivery is unsupported", text)


if __name__ == "__main__":
    unittest.main()
