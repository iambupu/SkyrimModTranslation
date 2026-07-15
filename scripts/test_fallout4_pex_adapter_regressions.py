from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PEX_PROJECT = ROOT / "adapters" / "SkyrimPexStringTool" / "SkyrimPexStringTool.csproj"
PEX_DLL = ROOT / "adapters" / "SkyrimPexStringTool" / "bin" / "Debug" / "net8.0" / "SkyrimPexStringTool.dll"
sys.path.insert(0, str(SCRIPTS))

import invoke_mutagen_pex_string_tool as invoke_tool  # noqa: E402
import new_final_binary_review_packet as binary_review  # noqa: E402
import prepare_pex_tool_output as prepare_output  # noqa: E402
import run_non_gui_translation_workflow as workflow  # noqa: E402
import verify_pex_output as verify_output  # noqa: E402


FIXTURE_PROJECT = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Mutagen.Bethesda.Core" Version="0.53.1" />
  </ItemGroup>
</Project>
"""


FIXTURE_SOURCE = r"""
using Mutagen.Bethesda;
using Mutagen.Bethesda.Pex;

if (args.Length != 3)
{
    Console.Error.WriteLine("Usage: FixtureBuilder <output> <game> <variant>");
    return 2;
}

var output = Path.GetFullPath(args[0]);
var category = args[1] switch
{
    "skyrim-se" => GameCategory.Skyrim,
    "fallout4" => GameCategory.Fallout4,
    _ => throw new ArgumentException($"unsupported fixture game: {args[1]}")
};
var variant = args[2];
var shared = "Shared visible text";
var visible = variant.StartsWith("tamper-", StringComparison.Ordinal)
    ? "Target visible text"
    : shared;

var pex = new PexFile(category)
{
    MajorVersion = 3,
    MinorVersion = category == GameCategory.Fallout4 ? (byte)9 : (byte)2,
    GameId = category == GameCategory.Fallout4 ? (ushort)2 : (ushort)1,
    CompilationTime = variant == "tamper-header"
        ? DateTime.UnixEpoch.AddSeconds(1)
        : DateTime.UnixEpoch,
    SourceFileName = "SyntheticFixture.psc",
    Username = "fixture",
    MachineName = "fixture",
};
var debugInfo = new DebugInfo(category) { ModificationTime = DateTime.UnixEpoch };
debugInfo.Functions.Add(new DebugFunction
{
    ObjectName = "SyntheticFixture",
    StateName = "",
    FunctionName = variant == "debug" ? shared : "DebugFunction",
    FunctionType = DebugFunctionType.Method,
});
pex.DebugInfo = debugInfo;
var obj = new PexObject
{
    Name = "SyntheticFixture",
    ParentClassName = "Quest",
    DocString = variant switch
    {
        "metadata" => shared,
        _ => "Stable metadata",
    },
    AutoStateName = "",
};
var state = new PexObjectState { Name = "" };
var firstFunction = MakeFunction("First", visible, InstructionOpcode.ASSIGN);
if (variant == "tamper-flags")
{
    firstFunction.Function!.Flags = FunctionFlags.NativeFunction;
}
state.Functions.Add(firstFunction);
if (variant == "shared")
{
    state.Functions.Add(MakeFunction("Second", shared, InstructionOpcode.ASSIGN));
}
if (variant == "cmp")
{
    state.Functions.Add(MakeFunction("Compare", shared, InstructionOpcode.CMP_EQ));
}
if (variant == "identifier")
{
    var identifierBody = MakeBody("Other visible text", InstructionOpcode.ASSIGN);
    identifierBody.Instructions[0].Arguments[0].StringValue = shared;
    state.Functions.Add(new PexObjectNamedFunction
    {
        FunctionName = "IdentifierUse",
        Function = identifierBody,
    });
}
obj.States.Add(state);
obj.Variables.Add(new PexObjectVariable
{
    Name = "FixtureCount",
    TypeName = "Int",
    VariableData = new PexObjectVariableData
    {
        VariableType = VariableType.Integer,
        IntValue = variant == "tamper-non-string" ? 8 : 7,
    },
});
var readHandler = MakeBody("Property handler visible text", InstructionOpcode.ASSIGN);
if (variant == "property-metadata")
{
    readHandler.DocString = shared;
}
obj.Properties.Add(new PexObjectProperty
{
    Name = "FixtureProperty",
    TypeName = "String",
    DocString = "",
    Flags = PropertyFlags.Read | PropertyFlags.Write,
    ReadHandler = readHandler,
    WriteHandler = MakeBody("Property setter visible text", InstructionOpcode.ASSIGN),
});
pex.Objects.Add(obj);
Directory.CreateDirectory(Path.GetDirectoryName(output)!);
pex.WritePexFile(output, category);
return 0;

static PexObjectNamedFunction MakeFunction(string name, string source, InstructionOpcode opcode)
{
    return new PexObjectNamedFunction
    {
        FunctionName = name,
        Function = MakeBody(source, opcode),
    };
}

static PexObjectFunction MakeBody(string source, InstructionOpcode opcode)
{
    var function = new PexObjectFunction
    {
        ReturnTypeName = "None",
        DocString = "",
    };
    var instruction = new PexObjectFunctionInstruction { OpCode = opcode };
    instruction.Arguments.Add(new PexObjectVariableData
    {
        VariableType = VariableType.Identifier,
        StringValue = "::temp",
    });
    instruction.Arguments.Add(new PexObjectVariableData
    {
        VariableType = VariableType.String,
        StringValue = source,
    });
    if (opcode == InstructionOpcode.CMP_EQ)
    {
        instruction.Arguments.Add(new PexObjectVariableData
        {
            VariableType = VariableType.String,
            StringValue = "Comparison peer",
        });
    }
    function.Instructions.Add(instruction);
    return function;
}
"""


def discover_dotnet() -> Path | None:
    candidates: list[Path] = []
    configured = os.environ.get("DOTNET_HOST_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))
    path_host = shutil.which("dotnet")
    if path_host:
        candidates.append(Path(path_host))
    config_path = ROOT / "config" / "tools.local.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            configured_path = str(config.get("DecoderTools", {}).get("DotNetSdkPath") or "").strip()
        except (OSError, ValueError, AttributeError):
            configured_path = ""
        if configured_path:
            candidate = Path(configured_path)
            candidates.append(candidate if candidate.is_absolute() else ROOT / candidate)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "--list-sdks"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        major_versions: list[int] = []
        for line in result.stdout.splitlines():
            version = line.strip().split(maxsplit=1)[0] if line.strip() else ""
            try:
                major_versions.append(int(version.split(".", maxsplit=1)[0]))
            except ValueError:
                continue
        if result.returncode == 0 and any(version >= 8 for version in major_versions):
            return candidate
    return None


DOTNET = discover_dotnet()


def marker(game_id: str | None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2 if game_id else 1,
        "kind": "bethesda-mod-chs-translation-workspace",
        "plugin_name": "skyrim-mod-chs-translation",
    }
    if game_id:
        value["game_id"] = game_id
        value["game_profile"] = game_id
    return value


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = ROOT / ".tmp" / "task-4-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name)
        for relative in (
            "work/extracted_mods/TestMod/Scripts",
            "work/normalized/TestMod",
            "translated/pex_visible_strings/TestMod",
            "translated/tool_outputs/TestMod/Scripts",
            "source/pex_exports/TestMod",
            "out/TestMod/tool_outputs/Scripts",
            "qa",
            "config",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)

    def write_marker(self, game_id: str | None) -> None:
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker(game_id), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def plugin_env(self) -> mock._patch_dict[str, str]:
        return mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
                "SKYRIM_CHS_WORKSPACE_ROOT": str(self.workspace),
            },
        )


class PexWrapperRegressionTests(WorkspaceTestCase):
    def invoke_wrapper(
        self,
        game_id: str | None,
        *,
        mode: str = "Export",
        explicit_game: str = "",
        allow_experimental: bool = False,
        apply_capability_level: str = "",
        include_apply_receipt: bool = True,
        apply_status: str = "success",
        apply_operation: str = "apply",
        apply_adapter_id: str = "mutagen-pex",
        apply_game_id: str = "",
        apply_artifact_path: str = "",
        apply_artifact_hash: str = "",
        tamper_apply_report: bool = False,
        export_mod_name: str = "TestMod",
        apply_lineage_mod_name: str | None = None,
        apply_lineage_translation_mod: str = "TestMod",
    ) -> tuple[int, list[str]]:
        self.write_marker(game_id)
        input_pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        input_pex.write_bytes(b"fixture")
        translation.write_text("", encoding="utf-8")
        config = self.workspace / "config/tools.local.json"
        config.write_text("{}\n", encoding="utf-8")
        fake_dotnet = self.workspace / "tools/dotnet-sdk/dotnet.exe"
        fake_dll = self.workspace / "tools/cache/SkyrimPexStringTool.dll"
        fake_dotnet.parent.mkdir(parents=True, exist_ok=True)
        fake_dll.parent.mkdir(parents=True, exist_ok=True)
        fake_dotnet.write_bytes(b"")
        fake_dll.write_bytes(b"")

        argv = [
            "invoke_mutagen_pex_string_tool.py",
            "--mode",
            mode,
            "--input-pex-path",
            str(input_pex),
            "--report-path",
            str(self.workspace / "qa/Test.pex.md"),
        ]
        if mode == "Export":
            export_output = (
                self.workspace
                / "source"
                / "pex_exports"
                / export_mod_name
                / "Test.pex_strings.jsonl"
            )
            export_output.parent.mkdir(parents=True, exist_ok=True)
            argv.extend(
                [
                    "--output-jsonl-path",
                    str(export_output),
                ]
            )
        else:
            output_pex = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
            if mode == "Verify":
                output_pex.write_bytes(b"output-fixture")
            argv.extend(
                [
                    "--translation-jsonl-path",
                    str(translation),
                    "--output-pex-path",
                    str(output_pex),
                ]
            )
            if mode == "Verify" and include_apply_receipt:
                effective_game = game_id or "skyrim-se"
                capability_level = apply_capability_level or (
                    "experimental_write" if effective_game == "fallout4" else "stable"
                )
                evidence_game = apply_game_id or effective_game
                apply_report = self.workspace / "qa/Test.apply.md"
                apply_report.write_text(
                    f"- game_id: {evidence_game}\n"
                    f"- capability_level: {capability_level}\n",
                    encoding="utf-8",
                )
                apply_receipt = self.workspace / "qa/Test.apply.adapter_result.json"
                receipt_payload = {
                    "status": apply_status,
                    "error_code": None if apply_status == "success" else "adapter_failed",
                    "operation": apply_operation,
                    "adapter_id": apply_adapter_id,
                    "artifacts": [
                        {
                            "path": apply_artifact_path
                            or output_pex.relative_to(self.workspace).as_posix(),
                            "sha256": apply_artifact_hash
                            or hashlib.sha256(output_pex.read_bytes()).hexdigest(),
                        },
                        {
                            "path": apply_report.relative_to(self.workspace).as_posix(),
                            "sha256": hashlib.sha256(apply_report.read_bytes()).hexdigest(),
                        },
                    ],
                    "evidence_files": [
                        apply_report.relative_to(self.workspace).as_posix()
                    ],
                    "warnings": (
                        ["experimental writeback"]
                        if capability_level == "experimental_write"
                        else []
                    ),
                    "blockers": [],
                }
                if apply_lineage_mod_name is not None:
                    lineage_translation = (
                        self.workspace
                        / "work"
                        / "normalized"
                        / apply_lineage_translation_mod
                        / "Test.translation.jsonl"
                    )
                    lineage_translation.parent.mkdir(parents=True, exist_ok=True)
                    if not lineage_translation.is_file():
                        lineage_translation.write_text("{}\n", encoding="utf-8")
                    receipt_payload.update(
                        {
                            "mod_name": apply_lineage_mod_name,
                            "inputs": [
                                {
                                    "path": input_pex.relative_to(self.workspace).as_posix(),
                                    "sha256": hashlib.sha256(input_pex.read_bytes()).hexdigest(),
                                },
                                {
                                    "path": lineage_translation.relative_to(self.workspace).as_posix(),
                                    "sha256": hashlib.sha256(
                                        lineage_translation.read_bytes()
                                    ).hexdigest(),
                                },
                            ],
                        }
                    )
                apply_receipt.write_text(
                    json.dumps(
                        receipt_payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if tamper_apply_report:
                    apply_report.write_text(
                        apply_report.read_text(encoding="utf-8") + "- tampered: True\n",
                        encoding="utf-8",
                    )
                argv.extend(
                    ["--apply-adapter-result-path", str(apply_receipt)]
                )
        if explicit_game:
            argv.extend(["--game", explicit_game])
        if allow_experimental:
            argv.append("--allow-experimental-writeback")

        def fake_run(command, **_kwargs):
            report_path = Path(command[command.index("--report") + 1])
            report_path.write_text(
                f"- game_id: {game_id or 'skyrim-se'}\n"
                f"- capability_level: "
                f"{command[command.index('--capability-level') + 1]}\n",
                encoding="utf-8",
            )
            if mode == "Export":
                Path(command[command.index("--output-jsonl") + 1]).write_text(
                    "{}\n", encoding="utf-8"
                )
            elif mode == "Apply" and "--dry-run" not in command:
                Path(command[command.index("--output-pex") + 1]).write_bytes(
                    b"output-fixture"
                )
            return subprocess.CompletedProcess([], 0)

        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=fake_dll),
            mock.patch.object(invoke_tool.subprocess, "run", side_effect=fake_run) as run,
        ):
            code = invoke_tool.main()
        return code, list(run.call_args.args[0]) if run.called else []

    def test_marker_without_game_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required game_id"):
            self.invoke_wrapper(None)

    def test_fallout4_marker_passes_game_to_export(self) -> None:
        code, command = self.invoke_wrapper("fallout4")
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")
        self.assertEqual(command[command.index("--pex-category") + 1], "Fallout4")
        self.assertEqual(
            command[command.index("--capability-level") + 1],
            "experimental_write",
        )

    def test_export_rejects_cross_mod_output_without_overwrite(self) -> None:
        target = self.workspace / "source/pex_exports/OtherMod/Test.pex_strings.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other-mod-export")

        with self.assertRaisesRegex(ValueError, "OutputJsonlPath"):
            self.invoke_wrapper(
                "fallout4",
                export_mod_name="OtherMod",
            )

        self.assertEqual(target.read_bytes(), b"other-mod-export")

    def test_explicit_game_conflict_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflict|mismatch"):
            self.invoke_wrapper("fallout4", explicit_game="skyrim-se")

    def test_fallout4_apply_without_opt_in_stops_before_build_or_output_creation(self) -> None:
        self.write_marker("fallout4")
        input_pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        output = self.workspace / "out/Blocked/Nested/Test.pex"
        report = self.workspace / "qa/Test.blocked.apply.md"
        input_pex.write_bytes(b"fixture")
        translation.write_text("", encoding="utf-8")
        output.parent.mkdir(parents=True)
        output.write_bytes(b"stale-output")
        report.write_text("stale-report\n", encoding="utf-8")
        argv = [
            "invoke_mutagen_pex_string_tool.py",
            "--mode",
            "Apply",
            "--input-pex-path",
            str(input_pex),
            "--translation-jsonl-path",
            str(translation),
            "--output-pex-path",
            str(output),
            "--report-path",
            str(report),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path") as dotnet_path,
            mock.patch.object(invoke_tool, "ensure_adapter_dll") as ensure_adapter,
            mock.patch.object(invoke_tool.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "experimental"):
                invoke_tool.main()
        dotnet_path.assert_not_called()
        ensure_adapter.assert_not_called()
        run.assert_not_called()
        self.assertEqual(output.read_bytes(), b"stale-output")
        self.assertEqual(report.read_text(encoding="utf-8"), "stale-report\n")

    def test_explicit_result_records_missing_experimental_confirmation_as_blocked(self) -> None:
        self.write_marker("fallout4")
        input_pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        receipt = self.workspace / "qa/Test.blocked.adapter_result.json"
        input_pex.write_bytes(b"fixture")
        translation.write_text("{}\n", encoding="utf-8")
        argv = [
            "invoke_mutagen_pex_string_tool.py",
            "--mode", "Apply",
            "--input-pex-path", str(input_pex),
            "--translation-jsonl-path", str(translation),
            "--output-pex-path", str(output),
            "--report-path", str(self.workspace / "qa/Test.blocked.md"),
            "--adapter-result-path", str(receipt),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path") as dotnet_path,
            mock.patch.object(invoke_tool, "ensure_adapter_dll") as ensure_adapter,
            mock.patch.object(invoke_tool.subprocess, "run") as run,
        ):
            self.assertEqual(invoke_tool.main(), 2)
        dotnet_path.assert_not_called()
        ensure_adapter.assert_not_called()
        run.assert_not_called()
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(
            payload["error_code"], "experimental_confirmation_required"
        )

    def test_adapter_failure_keeps_report_cleans_binary_and_writes_error_result(self) -> None:
        self.write_marker("skyrim-se")
        input_pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        report = self.workspace / "qa/Test.failed.apply.md"
        receipt = self.workspace / "qa/Test.failed.adapter_result.json"
        input_pex.write_bytes(b"input")
        translation.write_text("{}\n", encoding="utf-8")
        (self.workspace / "config/tools.local.json").write_text("{}\n", encoding="utf-8")
        fake_dotnet = self.workspace / "tools/dotnet-sdk/dotnet.exe"
        fake_dll = self.workspace / "tools/cache/SkyrimPexStringTool.dll"
        fake_dotnet.parent.mkdir(parents=True)
        fake_dll.parent.mkdir(parents=True)
        fake_dotnet.write_bytes(b"")
        fake_dll.write_bytes(b"")
        argv = [
            "invoke_mutagen_pex_string_tool.py",
            "--mode", "Apply",
            "--input-pex-path", str(input_pex),
            "--translation-jsonl-path", str(translation),
            "--output-pex-path", str(output),
            "--report-path", str(report),
            "--adapter-result-path", str(receipt),
        ]

        def fake_run(*_args, **_kwargs):
            output.write_bytes(b"partial")
            report.write_text("- failure: controlled adapter\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 7)

        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=fake_dll),
            mock.patch.object(invoke_tool.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(invoke_tool.main(), 1)
        self.assertFalse(output.exists())
        self.assertTrue(report.is_file())
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "adapter_failed")
        self.assertEqual(
            payload["evidence_files"], [report.relative_to(self.workspace).as_posix()]
        )

    def test_fallout4_apply_opt_in_is_forwarded(self) -> None:
        code, command = self.invoke_wrapper("fallout4", mode="Apply", allow_experimental=True)
        self.assertEqual(code, 0)
        self.assertIn("--allow-experimental-writeback", command)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")
        self.assertEqual(command[command.index("--pex-category") + 1], "Fallout4")
        self.assertEqual(
            command[command.index("--capability-level") + 1],
            "experimental_write",
        )

    def test_fallout4_verify_is_read_only_and_does_not_require_writeback_opt_in(self) -> None:
        code, command = self.invoke_wrapper("fallout4", mode="Verify")
        self.assertEqual(code, 0)
        self.assertEqual(command[0:2], [str(self.workspace / "tools/dotnet-sdk/dotnet.exe"), str(self.workspace / "tools/cache/SkyrimPexStringTool.dll")])
        self.assertEqual(command[2], "verify")
        self.assertNotIn("--allow-experimental-writeback", command)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")
        self.assertEqual(command[command.index("--pex-category") + 1], "Fallout4")
        self.assertEqual(
            command[command.index("--capability-level") + 1],
            "experimental_write",
        )

    def test_verify_rejects_new_receipt_cross_mod_lineage(self) -> None:
        with self.assertRaisesRegex(ValueError, "Mod lane"):
            self.invoke_wrapper(
                "skyrim-se",
                mode="Verify",
                apply_lineage_mod_name="OtherMod",
            )

    def test_verify_rejects_new_receipt_input_lineage_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "input lineage"):
            self.invoke_wrapper(
                "skyrim-se",
                mode="Verify",
                apply_lineage_mod_name="TestMod",
                apply_lineage_translation_mod="OtherMod",
            )

    def test_verify_rejects_apply_capability_evidence_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.invoke_wrapper(
                "fallout4",
                mode="Verify",
                apply_capability_level="stable",
            )

    def test_skyrim_verify_requires_apply_adapter_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --apply-adapter-result-path"):
            self.invoke_wrapper(
                "skyrim-se",
                mode="Verify",
                include_apply_receipt=False,
            )

    def test_verify_rejects_apply_receipt_contract_mismatches(self) -> None:
        cases = (
            ({"apply_status": "error"}, "successful Apply"),
            ({"apply_operation": "verify"}, "successful Apply"),
            ({"apply_adapter_id": "bethesda-ba2"}, "adapter_id"),
            ({"apply_game_id": "fallout4"}, "game_id"),
            ({"apply_artifact_path": "out/Other/tool_outputs/Scripts/Test.pex"}, "output PEX hash"),
            ({"apply_artifact_hash": "0" * 64}, "output PEX hash"),
            ({"apply_capability_level": "experimental_write"}, "does not match"),
        )
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, message):
                    self.invoke_wrapper("skyrim-se", mode="Verify", **kwargs)

    def test_verify_rejects_tampered_apply_evidence_report(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence.*hash|hash.*evidence"):
            self.invoke_wrapper(
                "skyrim-se",
                mode="Verify",
                tamper_apply_report=True,
            )

    def test_apply_writes_standard_result_only_when_explicitly_requested(self) -> None:
        self.write_marker("fallout4")
        input_pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        report = self.workspace / "qa/Test.apply.md"
        receipt = self.workspace / "qa/Test.apply.adapter_result.json"
        input_pex.write_bytes(b"input")
        translation.write_text("{}\n", encoding="utf-8")
        (self.workspace / "config/tools.local.json").write_text("{}\n", encoding="utf-8")
        fake_dotnet = self.workspace / "tools/dotnet-sdk/dotnet.exe"
        fake_dll = self.workspace / "tools/cache/SkyrimPexStringTool.dll"
        fake_dotnet.parent.mkdir(parents=True)
        fake_dll.parent.mkdir(parents=True)
        fake_dotnet.write_bytes(b"")
        fake_dll.write_bytes(b"")
        argv = [
            "invoke_mutagen_pex_string_tool.py",
            "--mode", "Apply",
            "--game", "fallout4",
            "--input-pex-path", str(input_pex),
            "--translation-jsonl-path", str(translation),
            "--output-pex-path", str(output),
            "--report-path", str(report),
            "--adapter-result-path", str(receipt),
            "--allow-experimental-writeback",
        ]

        def fake_run(*_args, **_kwargs):
            output.write_bytes(b"generated-pex")
            report.write_text(
                "- game_id: fallout4\n- capability_level: experimental_write\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess([], 0)

        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=fake_dll),
            mock.patch.object(invoke_tool.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(invoke_tool.main(), 0)

        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["operation"], "apply")
        self.assertEqual(payload["adapter_id"], "mutagen-pex")
        evidence_path = report.relative_to(self.workspace).as_posix()
        evidence_artifact = next(
            item for item in payload["artifacts"] if item["path"] == evidence_path
        )
        self.assertEqual(
            evidence_artifact["sha256"], hashlib.sha256(report.read_bytes()).hexdigest()
        )
        self.assertTrue(payload["warnings"])
        self.assertEqual(payload["artifacts"][0]["path"], output.relative_to(self.workspace).as_posix())
        self.assertEqual(
            payload["artifacts"][0]["sha256"],
            hashlib.sha256(output.read_bytes()).hexdigest(),
        )


class PexWorkflowAndMetadataRegressionTests(WorkspaceTestCase):
    def write_valid_experimental_apply_report(
        self,
        path: Path,
        original: Path,
        output: Path,
        translation: Path,
    ) -> None:
        path.write_text(
            "\n".join(
                [
                    "- game_id: fallout4",
                    "- pex_category: Fallout4",
                    "- writeback_status: experimental",
                    "- experimental_opt_in: True",
                    f"- Input PEX: {verify_output.relative_path(self.workspace, original)}",
                    f"- Translation JSONL: {verify_output.relative_path(self.workspace, translation)}",
                    f"- Output PEX: {verify_output.relative_path(self.workspace, output)}",
                    f"- Input SHA256: {verify_output.sha256_file(original)}",
                    f"- Translation JSONL SHA256: {verify_output.sha256_file(translation)}",
                    f"- Output SHA256: {verify_output.sha256_file(output)}",
                    "- Validation errors: 0",
                    "- Conflicting source rows: 0",
                    "- Missing usable rows: 0",
                    "- Input objects: 1",
                    "- Output objects: 1",
                    "- Input states: 1",
                    "- Output states: 1",
                    "- Input functions: 1",
                    "- Output functions: 1",
                    "- Input instructions: 1",
                    "- Output instructions: 1",
                    "- Structure preserved: True",
                    "- Output published: True",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_workflow_blocks_fallout4_apply_without_running_adapter(self) -> None:
        self.write_marker("fallout4")
        pex = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        pex.write_bytes(b"fixture")
        translation = self.workspace / "translated/pex_visible_strings/TestMod/Test.translation.jsonl"
        translation.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": "fallout4",
                    "ModName": "Test.pex",
                    "Source": "Visible notification text",
                    "Result": "可见通知文本",
                    "risk": "candidate",
                    "Context": "Debug.Notification visible text",
                    "object_name": "Fixture",
                    "state_name": "",
                    "function_name": "Run",
                    "opcode": "ASSIGN",
                    "instruction_index": 0,
                    "argument_index": 1,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        steps: list[workflow.Step] = []
        issues: list[workflow.Issue] = []
        with (
            self.plugin_env(),
            mock.patch.object(workflow, "collect_pex_translation_inputs", return_value=[translation]),
            mock.patch.object(workflow, "run_python_script") as run,
        ):
            ok = workflow.run_pex_translation_stage(
                self.workspace,
                steps,
                issues,
                "TestMod",
                self.workspace / "work/extracted_mods/TestMod",
            )
        self.assertFalse(ok)
        run.assert_not_called()
        self.assertTrue(any("experimental" in issue.Message.lower() for issue in issues))

    def test_verify_parse_check_forwards_game(self) -> None:
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        output.write_bytes(b"fixture")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(verify_output.subprocess, "run", return_value=completed) as run:
            verify_output.verify_output_parseable(self.workspace, output, "fallout4")
        command = list(run.call_args.args[0])
        self.assertEqual(command[command.index("--game") + 1], "fallout4")

    def test_fallout4_verification_rejects_missing_experimental_apply_report(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        report = self.workspace / "qa/Test.verify.md"
        original.write_bytes(b"original")
        output.write_bytes(b"translated")
        translation.write_text("", encoding="utf-8")
        argv = [
            "verify_pex_output.py",
            "--game",
            "fallout4",
            "--original-pex-path",
            str(original),
            "--output-pex-path",
            str(output),
            "--translation-jsonl-path",
            str(translation),
            "--report-output-path",
            str(report),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(verify_output, "project_root", return_value=self.workspace),
            mock.patch.object(
                verify_output,
                "verify_output_parseable",
                return_value=(self.workspace / "source/check.jsonl", self.workspace / "qa/check.md", ""),
            ),
        ):
            code = verify_output.main()
        self.assertEqual(code, 1)
        self.assertIn("experimental", report.read_text(encoding="utf-8").lower())

    def test_fallout4_warn_only_cannot_bypass_missing_experimental_report(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        report = self.workspace / "qa/Test.warn-only.verify.md"
        original.write_bytes(b"original")
        output.write_bytes(b"translated")
        translation.write_text("", encoding="utf-8")
        argv = [
            "verify_pex_output.py",
            "--game",
            "fallout4",
            "--original-pex-path",
            str(original),
            "--output-pex-path",
            str(output),
            "--translation-jsonl-path",
            str(translation),
            "--report-output-path",
            str(report),
            "--warn-only",
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(verify_output, "project_root", return_value=self.workspace),
            mock.patch.object(
                verify_output,
                "verify_output_parseable",
                return_value=(self.workspace / "source/check.jsonl", self.workspace / "qa/check.md", ""),
            ),
        ):
            self.assertEqual(verify_output.main(), 1)

    def test_fallout4_verification_rejects_structure_count_mismatch(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        apply_report = self.workspace / "qa/Test.structure.apply.md"
        for path in (original, output, translation):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        apply_report.write_text(
            "\n".join(
                [
                    "- game_id: fallout4",
                    "- pex_category: Fallout4",
                    "- writeback_status: experimental",
                    "- experimental_opt_in: True",
                    "- Input PEX: work/extracted_mods/TestMod/Scripts/Test.pex",
                    "- Translation JSONL: work/normalized/TestMod/Test.translation.jsonl",
                    "- Output PEX: out/TestMod/tool_outputs/Scripts/Test.pex",
                    "- Validation errors: 0",
                    "- Conflicting source rows: 0",
                    "- Missing usable rows: 0",
                    "- Input objects: 1",
                    "- Output objects: 1",
                    "- Input states: 1",
                    "- Output states: 1",
                    "- Input functions: 2",
                    "- Output functions: 1",
                    "- Input instructions: 2",
                    "- Output instructions: 2",
                    "- Structure preserved: True",
                    "- Output published: True",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        issues: list[str] = []
        with self.plugin_env():
            context = verify_output.resolve_game_context(self.workspace, "fallout4")
        verify_output.validate_experimental_apply_report(
            apply_report,
            context,
            self.workspace,
            original,
            output,
            translation,
            issues,
        )
        self.assertTrue(any("functions" in issue for issue in issues))

    def test_fallout4_verification_rejects_apply_report_for_other_output(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        report = self.workspace / "qa/Test.stale.verify.md"
        apply_report = self.workspace / "qa/Test.stale.apply.md"
        original.write_bytes(b"original")
        output.write_bytes(b"translated")
        translation.write_text("", encoding="utf-8")
        apply_report.write_text(
            "\n".join(
                [
                    "- game_id: fallout4",
                    "- pex_category: Fallout4",
                    "- writeback_status: experimental",
                    "- experimental_opt_in: True",
                    "- Input PEX: work/extracted_mods/Other/Scripts/Other.pex",
                    "- Output PEX: out/Other/tool_outputs/Scripts/Other.pex",
                    "- Validation errors: 0",
                    "- Conflicting source rows: 0",
                    "- Missing usable rows: 0",
                    "- Input objects: 1",
                    "- Output objects: 1",
                    "- Input states: 1",
                    "- Output states: 1",
                    "- Input functions: 1",
                    "- Output functions: 1",
                    "- Input instructions: 1",
                    "- Output instructions: 1",
                    "- Structure preserved: True",
                    "- Output published: True",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        argv = [
            "verify_pex_output.py",
            "--game",
            "fallout4",
            "--original-pex-path",
            str(original),
            "--output-pex-path",
            str(output),
            "--translation-jsonl-path",
            str(translation),
            "--report-output-path",
            str(report),
            "--apply-report-path",
            str(apply_report),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(verify_output, "project_root", return_value=self.workspace),
            mock.patch.object(
                verify_output,
                "verify_output_parseable",
                return_value=(self.workspace / "source/check.jsonl", self.workspace / "qa/check.md", ""),
            ),
        ):
            self.assertEqual(verify_output.main(), 1)
        self.assertIn("refers to", report.read_text(encoding="utf-8").lower())

    def test_fallout4_verification_binds_apply_report_to_current_file_hashes(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        apply_report = self.workspace / "qa/Test.hashes.apply.md"
        original.write_bytes(b"original-a")
        output.write_bytes(b"output-a")
        translation.write_bytes(b"translation-a")
        self.write_valid_experimental_apply_report(apply_report, original, output, translation)

        context = verify_output.resolve_game_context(self.workspace, "fallout4")
        issues: list[str] = []
        verify_output.validate_experimental_apply_report(
            apply_report, context, self.workspace, original, output, translation, issues
        )
        self.assertEqual(issues, [])

        for changed_path, replacement in (
            (original, b"original-b"),
            (translation, b"translation-b"),
            (output, b"output-b"),
        ):
            with self.subTest(path=changed_path.name):
                previous = changed_path.read_bytes()
                changed_path.write_bytes(replacement)
                drift_issues: list[str] = []
                verify_output.validate_experimental_apply_report(
                    apply_report,
                    context,
                    self.workspace,
                    original,
                    output,
                    translation,
                    drift_issues,
                )
                self.assertTrue(any("sha256" in issue.lower() for issue in drift_issues))
                changed_path.write_bytes(previous)

    def test_fallout4_verification_rejects_missing_or_malformed_apply_hashes(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        apply_report = self.workspace / "qa/Test.fake.apply.md"
        for path in (original, output, translation):
            path.write_bytes(b"fixture")
        self.write_valid_experimental_apply_report(apply_report, original, output, translation)
        report_text = apply_report.read_text(encoding="utf-8")
        context = verify_output.resolve_game_context(self.workspace, "fallout4")

        for variant in ("missing", "malformed"):
            with self.subTest(variant=variant):
                if variant == "missing":
                    changed = "\n".join(
                        line for line in report_text.splitlines() if "SHA256:" not in line
                    ) + "\n"
                else:
                    changed = report_text.replace(
                        f"- Output SHA256: {verify_output.sha256_file(output)}",
                        "- Output SHA256: not-a-hash",
                    )
                apply_report.write_text(changed, encoding="utf-8")
                issues: list[str] = []
                verify_output.validate_experimental_apply_report(
                    apply_report,
                    context,
                    self.workspace,
                    original,
                    output,
                    translation,
                    issues,
                )
                self.assertTrue(any("sha256" in issue.lower() for issue in issues))

    def test_verification_report_records_apply_report_sha256(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        apply_report = self.workspace / "qa/Test.valid.apply.md"
        verification_report = self.workspace / "qa/Test.valid.verify.md"
        original.write_bytes(b"source-visible")
        output.write_bytes("目标文本".encode("utf-8"))
        translation.write_text(
            json.dumps({"Source": "source-visible", "Result": "目标文本", "risk": "candidate"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.write_valid_experimental_apply_report(apply_report, original, output, translation)
        argv = [
            "verify_pex_output.py",
            "--game", "fallout4",
            "--original-pex-path", str(original),
            "--output-pex-path", str(output),
            "--translation-jsonl-path", str(translation),
            "--apply-report-path", str(apply_report),
            "--report-output-path", str(verification_report),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(verify_output, "project_root", return_value=self.workspace),
            mock.patch.object(
                verify_output,
                "verify_output_parseable",
                return_value=(self.workspace / "source/check.jsonl", self.workspace / "qa/check.md", ""),
            ),
            mock.patch.object(
                verify_output,
                "verify_output_independently",
                return_value=(self.workspace / "qa/fresh.md", ""),
            ),
        ):
            self.assertEqual(verify_output.main(), 0)
        self.assertIn(
            f"- Apply report SHA256: {verify_output.sha256_file(apply_report)}",
            verification_report.read_text(encoding="utf-8"),
        )

    def test_verifier_rejects_dangerous_path_collisions_without_touching_files(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        translation = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        report = self.workspace / "qa/Test.verify.md"
        apply_report = self.workspace / "qa/Test.apply.md"
        fixtures = {
            original: b"original",
            output: b"output",
            translation: b"translation",
            report: b"report",
            apply_report: b"apply-report",
        }
        for path, content in fixtures.items():
            path.write_bytes(content)

        evidence_key = verify_output.hashlib.sha256(
            "\0".join(
                [
                    "fallout4",
                    str(original.resolve(strict=False)).casefold(),
                    str(output.resolve(strict=False)).casefold(),
                    str(translation.resolve(strict=False)).casefold(),
                ]
            ).encode("utf-8")
        ).hexdigest()[:16]
        independent_report = (
            self.workspace
            / "qa/_pex_independent_checks"
            / f"Test.{evidence_key}.md"
        )
        independent_report.parent.mkdir(parents=True, exist_ok=True)
        independent_report.write_bytes(b"independent-apply-report")
        fixtures[independent_report] = b"independent-apply-report"

        parse_key = verify_output.hashlib.sha256(
            "\0".join(
                [
                    "fallout4",
                    str(output.resolve(strict=False)).casefold(),
                ]
            ).encode("utf-8")
        ).hexdigest()[:16]
        parse_jsonl = (
            self.workspace
            / "source/pex_exports/_verification"
            / f"Test.{parse_key}.pex_strings.jsonl"
        )
        parse_report = (
            self.workspace
            / "qa/_pex_parse_checks"
            / f"Test.{parse_key}.md"
        )
        parse_jsonl.parent.mkdir(parents=True, exist_ok=True)
        parse_report.parent.mkdir(parents=True, exist_ok=True)
        parse_jsonl.write_bytes(b"parse-jsonl")
        parse_report.write_bytes(b"parse-report")
        fixtures[parse_jsonl] = b"parse-jsonl"
        fixtures[parse_report] = b"parse-report"

        cases = (
            ("output-report", original, output, translation, output, apply_report),
            ("apply-report", original, output, translation, report, report),
            ("original-output", original, original, translation, report, apply_report),
            ("translation-output", original, output, output, report, apply_report),
            ("independent-apply-report", original, output, translation, report, independent_report),
            ("parse-report-apply", original, output, translation, report, parse_report),
            ("parse-jsonl-translation", original, output, parse_jsonl, report, apply_report),
        )
        for name, original_arg, output_arg, translation_arg, report_arg, apply_arg in cases:
            with self.subTest(case=name):
                argv = [
                    "verify_pex_output.py",
                    "--game", "fallout4",
                    "--original-pex-path", str(original_arg),
                    "--output-pex-path", str(output_arg),
                    "--translation-jsonl-path", str(translation_arg),
                    "--report-output-path", str(report_arg),
                    "--apply-report-path", str(apply_arg),
                ]
                with (
                    self.plugin_env(),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(verify_output, "project_root", return_value=self.workspace),
                ):
                    with self.assertRaisesRegex(ValueError, r"distinct|collision|\.md"):
                        verify_output.main()
                for path, content in fixtures.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_prepare_manifest_records_game_and_unwritten_copy_status(self) -> None:
        self.write_marker("fallout4")
        source = self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        visible = self.workspace / "work/normalized/TestMod/pex_visible_strings.jsonl"
        source.write_bytes(b"fixture")
        visible.write_text(
            json.dumps({"ModName": "Test.pex", "Source": "Visible text"}) + "\n",
            encoding="utf-8",
        )
        argv = [
            "prepare_pex_tool_output.py",
            "--mod-name",
            "TestMod",
            "--game",
            "fallout4",
            "--source-mod-dir",
            str(self.workspace / "work/extracted_mods/TestMod"),
            "--visible-strings-path",
            str(visible),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(prepare_output, "project_root", return_value=self.workspace),
        ):
            self.assertEqual(prepare_output.main(), 0)
        manifest = json.loads(
            (self.workspace / "out/TestMod/tool_outputs/meta/pex_writeback_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["game_id"], "fallout4")
        self.assertEqual(manifest["pex_category"], "Fallout4")
        self.assertEqual(manifest["pex_writeback_status"], "experimental")
        self.assertEqual(manifest["Copies"][0]["WritebackStatus"], "not_written_prepared_copy")

    def test_final_review_pex_identity_includes_game_and_source(self) -> None:
        base = {
            "game_id": "fallout4",
            "ModName": "Test.pex",
            "Source": "Visible text",
            "object_name": "Fixture",
            "state_name": "",
            "function_name": "Run",
            "opcode": "ASSIGN",
            "instruction_index": 0,
            "argument_index": 1,
        }
        other_game = {**base, "game_id": "skyrim-se"}
        other_source = {**base, "Source": "Changed source"}
        self.assertNotEqual(binary_review.pex_identity(base), binary_review.pex_identity(other_game))
        self.assertNotEqual(binary_review.pex_identity(base), binary_review.pex_identity(other_source))
        self.assertEqual(
            binary_review.pex_location_identity(base),
            binary_review.pex_location_identity(other_source),
        )

    def test_final_review_pex_export_uses_registry_entrypoint_and_game(self) -> None:
        pex = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        pex.write_bytes(b"fixture")
        completed = subprocess.CompletedProcess([], 0, "", "")
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                with mock.patch.object(
                    binary_review.subprocess, "run", return_value=completed
                ) as run:
                    result = binary_review.run_pex_export(
                        self.workspace,
                        pex,
                        f"source/pex_exports/TestMod/Test.{game_id}.jsonl",
                        f"qa/Test.{game_id}.export.md",
                        binary_review.load_game_profile(game_id),
                    )
                self.assertEqual(result.returncode, 0)
                command = list(run.call_args.args[0])
                self.assertEqual(Path(command[1]).name, "invoke_mutagen_pex_string_tool.py")
                self.assertEqual(command[command.index("--mode") + 1], "Export")
                self.assertEqual(command[command.index("--game") + 1], game_id)

    def test_final_review_pex_export_fails_closed_when_read_is_unsupported(self) -> None:
        pex = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        pex.write_bytes(b"fixture")
        context = binary_review.load_game_profile("skyrim-se")
        unsupported = replace(
            context,
            capabilities={
                name: spec for name, spec in context.capabilities.items() if name != "pex"
            },
        )
        with mock.patch.object(binary_review.subprocess, "run") as run:
            result = binary_review.run_pex_export(
                self.workspace,
                pex,
                "source/pex_exports/TestMod/Test.unsupported.jsonl",
                "qa/Test.unsupported.export.md",
                unsupported,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Capability 'pex'", result.stderr)
        run.assert_not_called()

    def test_final_review_pex_export_fails_closed_when_adapter_is_unavailable(self) -> None:
        pex = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        pex.write_bytes(b"fixture")
        context = binary_review.load_game_profile("skyrim-se")
        with (
            mock.patch.object(
                binary_review,
                "require_capability_script_entrypoint",
                side_effect=ValueError("adapter unavailable"),
            ),
            mock.patch.object(binary_review.subprocess, "run") as run,
        ):
            result = binary_review.run_pex_export(
                self.workspace,
                pex,
                "source/pex_exports/TestMod/Test.no-adapter.jsonl",
                "qa/Test.no-adapter.export.md",
                context,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("adapter unavailable", result.stderr)
        run.assert_not_called()

    def test_final_review_cache_binds_original_final_packet_and_items(self) -> None:
        original_root = self.workspace / "work/extracted_mods/TestMod"
        final_root = self.workspace / "out/TestMod/汉化产出/final_mod"
        original = original_root / "Scripts/Test.pex"
        final = final_root / "Scripts/Test.pex"
        packet = self.workspace / "qa/Test.packet.md"
        items = self.workspace / "qa/Test.items.jsonl"
        cache = self.workspace / "qa/Test.cache.json"
        final.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"original-a")
        final.write_bytes(b"final-a")
        packet.write_text("packet-a\n", encoding="utf-8")
        items.write_text("{}\n", encoding="utf-8")
        metadata = {"game_id": "fallout4", "support_level": "experimental"}
        final_fingerprints = binary_review.binary_fingerprints(final_root)
        original_fingerprints = binary_review.binary_fingerprints(original_root)
        binary_review.write_cache(
            cache,
            packet,
            items,
            final_fingerprints,
            original_fingerprints,
            metadata,
        )
        self.assertTrue(
            binary_review.cached_packet_is_current(
                cache,
                packet,
                items,
                final_fingerprints,
                original_fingerprints,
                metadata,
            )
        )

        mutations = (
            (original, b"original-b"),
            (final, b"final-b"),
            (packet, b"packet-b\n"),
            (items, b'{"tampered":true}\n'),
        )
        for changed_path, replacement in mutations:
            with self.subTest(path=changed_path.name):
                previous = changed_path.read_bytes()
                changed_path.write_bytes(replacement)
                self.assertFalse(
                    binary_review.cached_packet_is_current(
                        cache,
                        packet,
                        items,
                        binary_review.binary_fingerprints(final_root),
                        binary_review.binary_fingerprints(original_root),
                        metadata,
                    )
                )
                changed_path.write_bytes(previous)

        self.assertFalse(
            binary_review.cached_packet_is_current(
                cache,
                packet,
                items,
                final_fingerprints,
                original_fingerprints,
                {**metadata, "game_id": "skyrim-se"},
            )
        )

    def test_final_review_legacy_cache_fails_closed(self) -> None:
        packet = self.workspace / "qa/Test.packet.md"
        items = self.workspace / "qa/Test.items.jsonl"
        cache = self.workspace / "qa/Test.cache.json"
        packet.write_text("packet\n", encoding="utf-8")
        items.write_text("{}\n", encoding="utf-8")
        cache.write_text(
            json.dumps(
                {
                    "FinalBinaryFingerprints": {},
                    "ItemsSHA256": binary_review.file_sha256(items),
                    "GameContext": {"game_id": "fallout4"},
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(
            binary_review.cached_packet_is_current(
                cache,
                packet,
                items,
                {},
                {},
                {"game_id": "fallout4"},
            )
        )


@unittest.skipIf(DOTNET is None, "a .NET 8 SDK is required for synthetic PEX regression fixtures")
class PexAdapterSyntheticFixtureTests(WorkspaceTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        assert DOTNET is not None
        cls.helper_root = ROOT / ".tmp" / "task-4-pex-fixture-builder"
        cls.helper_root.mkdir(parents=True, exist_ok=True)
        (cls.helper_root / "FixtureBuilder.csproj").write_text(FIXTURE_PROJECT, encoding="utf-8")
        (cls.helper_root / "Program.cs").write_text(textwrap.dedent(FIXTURE_SOURCE), encoding="utf-8")
        for project in (cls.helper_root / "FixtureBuilder.csproj", PEX_PROJECT):
            result = subprocess.run(
                [str(DOTNET), "build", str(project), "--nologo"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(f"dotnet build failed for {project}:\n{result.stdout}\n{result.stderr}")
        cls.helper_dll = cls.helper_root / "bin/Debug/net8.0/FixtureBuilder.dll"

    def build_fixture(
        self,
        game: str,
        variant: str = "single",
        path: Path | None = None,
    ) -> Path:
        assert DOTNET is not None
        path = path or self.workspace / "work/extracted_mods/TestMod/Scripts/Test.pex"
        result = subprocess.run(
            [str(DOTNET), str(self.helper_dll), str(path), game, variant],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return path

    def run_adapter(
        self,
        *args: str,
        inject_contract: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        assert DOTNET is not None
        command_args = list(args)
        game = command_args[command_args.index("--game") + 1] if "--game" in command_args else ""
        if inject_contract and "--pex-category" not in command_args:
            pex_category = {
                "skyrim-se": "Skyrim",
                "fallout4": "Fallout4",
            }.get(game, "Unknown")
            command_args.extend(["--pex-category", pex_category])
        if inject_contract and "--capability-level" not in command_args:
            capability_level = "experimental_write" if game == "fallout4" else "stable"
            command_args.extend(["--capability-level", capability_level])
        return subprocess.run(
            [str(DOTNET), str(PEX_DLL), *command_args],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def test_all_commands_require_profile_category_and_capability_level(self) -> None:
        fixture = self.build_fixture("skyrim-se")
        input_before = fixture.read_bytes()
        translation = self.write_rows([])
        output_pex = self.workspace / "out/TestMod/tool_outputs/Scripts/Strict.pex"
        output_jsonl = self.workspace / "source/pex_exports/TestMod/Strict.jsonl"
        report = self.workspace / "qa/Strict.md"
        commands = {
            "export": [
                "export", "--game", "skyrim-se",
                "--pex-category", "Skyrim",
                "--capability-level", "stable",
                "--project-root", str(self.workspace),
                "--input-pex", str(fixture),
                "--output-jsonl", str(output_jsonl),
                "--report", str(report),
            ],
            "apply": [
                "apply", "--game", "skyrim-se",
                "--pex-category", "Skyrim",
                "--capability-level", "stable",
                "--project-root", str(self.workspace),
                "--input-pex", str(fixture),
                "--translation-jsonl", str(translation),
                "--output-pex", str(output_pex),
                "--report", str(report),
            ],
            "verify": [
                "verify", "--game", "skyrim-se",
                "--pex-category", "Skyrim",
                "--capability-level", "stable",
                "--project-root", str(self.workspace),
                "--input-pex", str(fixture),
                "--translation-jsonl", str(translation),
                "--output-pex", str(output_pex),
                "--report", str(report),
            ],
        }
        for command_name, command in commands.items():
            for required_flag in ("--pex-category", "--capability-level"):
                with self.subTest(command=command_name, missing=required_flag):
                    output_pex.unlink(missing_ok=True)
                    output_jsonl.unlink(missing_ok=True)
                    report.unlink(missing_ok=True)
                    candidate = command.copy()
                    index = candidate.index(required_flag)
                    del candidate[index:index + 2]
                    result = self.run_adapter(*candidate, inject_contract=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(required_flag, result.stderr)
                    self.assertEqual(fixture.read_bytes(), input_before)
                    self.assertFalse(output_pex.exists())
                    self.assertFalse(output_jsonl.exists())
                    self.assertFalse(report.exists())

    def test_direct_apply_rejects_forbidden_output_roles_without_modifying_files(self) -> None:
        fixture = self.build_fixture("skyrim-se")
        _, rows = self.export_fixture(fixture, "skyrim-se")
        translation = self.write_rows(self.translated_rows(rows))
        input_before = fixture.read_bytes()
        forbidden_outputs = (
            self.workspace / "mod/Forbidden.pex",
            self.workspace / "work/extracted_mods/TestMod/Scripts/Forbidden.pex",
            self.workspace / "source/Forbidden.pex",
        )
        for index, output in enumerate(forbidden_outputs):
            with self.subTest(output=output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"existing-forbidden-output")
                report = self.workspace / f"qa/Forbidden.{index}.md"
                result = self.run_adapter(
                    "apply",
                    "--game", "skyrim-se",
                    "--project-root", str(self.workspace),
                    "--input-pex", str(fixture),
                    "--translation-jsonl", str(translation),
                    "--output-pex", str(output),
                    "--report", str(report),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("output PEX", result.stderr)
                self.assertEqual(fixture.read_bytes(), input_before)
                self.assertEqual(output.read_bytes(), b"existing-forbidden-output")
                self.assertFalse(report.exists())

    def test_direct_apply_rolls_back_published_output_when_report_write_fails(self) -> None:
        fixture = self.build_fixture("skyrim-se")
        _, rows = self.export_fixture(fixture, "skyrim-se")
        translation = self.write_rows(self.translated_rows(rows))
        input_before = fixture.read_bytes()
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Rollback.pex"
        report = self.workspace / "qa/ReportAsDirectory.md"
        report.mkdir()

        result = self.run_adapter(
            "apply",
            "--game", "skyrim-se",
            "--project-root", str(self.workspace),
            "--input-pex", str(fixture),
            "--translation-jsonl", str(translation),
            "--output-pex", str(output),
            "--report", str(report),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.read_bytes(), input_before)
        self.assertFalse(output.exists())
        self.assertTrue(report.is_dir())

    def export_fixture(self, input_pex: Path, game: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        output = self.workspace / "source/pex_exports/TestMod/Test.pex_strings.jsonl"
        report = self.workspace / "qa/Test.export.md"
        result = self.run_adapter(
            "export",
            "--game",
            game,
            "--project-root",
            str(self.workspace),
            "--input-pex",
            str(input_pex),
            "--output-jsonl",
            str(output),
            "--report",
            str(report),
        )
        rows = []
        if output.is_file():
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8-sig").splitlines() if line]
        return result, rows

    def write_rows(self, rows: list[dict]) -> Path:
        path = self.workspace / "work/normalized/TestMod/Test.translation.jsonl"
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def apply_fixture(
        self,
        input_pex: Path,
        translation: Path,
        game: str,
        *,
        allow_experimental: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        report = self.workspace / "qa/Test.apply.md"
        args = [
            "apply",
            "--game",
            game,
            "--project-root",
            str(self.workspace),
            "--input-pex",
            str(input_pex),
            "--translation-jsonl",
            str(translation),
            "--output-pex",
            str(output),
            "--report",
            str(report),
        ]
        if allow_experimental:
            args.append("--allow-experimental-writeback")
        return self.run_adapter(*args), output, report

    def verify_fixture(
        self,
        original: Path,
        output: Path,
        translation: Path,
        game: str = "fallout4",
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        report = self.workspace / "qa/Test.fresh-verify.md"
        result = self.run_adapter(
            "verify",
            "--game", game,
            "--project-root", str(self.workspace),
            "--input-pex", str(original),
            "--output-pex", str(output),
            "--translation-jsonl", str(translation),
            "--report", str(report),
        )
        return result, report

    def write_fake_apply_report(
        self,
        original: Path,
        output: Path,
        translation: Path,
        *,
        functions: int = 3,
        instructions: int = 3,
    ) -> Path:
        report = self.workspace / "qa/Test.fake.apply.md"
        report.write_text(
            "\n".join(
                [
                    "- game_id: fallout4",
                    "- pex_category: Fallout4",
                    "- writeback_status: experimental",
                    "- experimental_opt_in: True",
                    f"- Input PEX: {verify_output.relative_path(self.workspace, original)}",
                    f"- Translation JSONL: {verify_output.relative_path(self.workspace, translation)}",
                    f"- Output PEX: {verify_output.relative_path(self.workspace, output)}",
                    f"- Input SHA256: {verify_output.sha256_file(original)}",
                    f"- Translation JSONL SHA256: {verify_output.sha256_file(translation)}",
                    f"- Output SHA256: {verify_output.sha256_file(output)}",
                    "- Validation errors: 0",
                    "- Conflicting source rows: 0",
                    "- Missing usable rows: 0",
                    "- Input objects: 1",
                    "- Output objects: 1",
                    "- Input states: 1",
                    "- Output states: 1",
                    f"- Input functions: {functions}",
                    f"- Output functions: {functions}",
                    f"- Input instructions: {instructions}",
                    f"- Output instructions: {instructions}",
                    "- Structure preserved: True",
                    "- Output published: True",
                ]
            ) + "\n",
            encoding="utf-8",
        )
        return report

    def run_python_verifier(
        self,
        original: Path,
        output: Path,
        translation: Path,
        apply_report: Path,
    ) -> tuple[int, Path]:
        report = self.workspace / "qa/Test.python-verify.md"
        argv = [
            "verify_pex_output.py",
            "--game", "fallout4",
            "--original-pex-path", str(original),
            "--output-pex-path", str(output),
            "--translation-jsonl-path", str(translation),
            "--apply-report-path", str(apply_report),
            "--report-output-path", str(report),
        ]
        with (
            self.plugin_env(),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(verify_output, "project_root", return_value=self.workspace),
            mock.patch.object(
                verify_output,
                "verify_output_parseable",
                return_value=(self.workspace / "source/check.jsonl", self.workspace / "qa/check.md", ""),
            ),
            mock.patch.object(
                verify_output,
                "verify_output_independently",
                side_effect=lambda *_: self._run_real_fresh_verifier(
                    original, output, translation
                ),
            ),
        ):
            return verify_output.main(), report

    def _run_real_fresh_verifier(
        self,
        original: Path,
        output: Path,
        translation: Path,
    ) -> tuple[Path, str]:
        result, report = self.verify_fixture(original, output, translation)
        error = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        return report, "" if result.returncode == 0 else error

    @staticmethod
    def visible_rows(rows: list[dict]) -> list[dict]:
        return [row for row in rows if row.get("Source") == "Shared visible text"]

    def translated_rows(self, rows: list[dict], target: str = "共享可见文本") -> list[dict]:
        result = []
        for row in self.visible_rows(rows):
            result.append({**row, "Result": target, "risk": "candidate"})
        return result

    def test_fallout4_export_emits_v2_game_metadata(self) -> None:
        fixture = self.build_fixture("fallout4")
        result, rows = self.export_fixture(fixture, "fallout4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(rows)
        self.assertTrue(all(row["schema_version"] == 2 for row in rows))
        self.assertTrue(all(row["game_id"] == "fallout4" for row in rows))

    def test_unknown_game_is_rejected_before_input_parse(self) -> None:
        result = self.run_adapter("export", "--game", "unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported pex category", result.stderr.lower())

    def test_fallout4_apply_requires_direct_cli_opt_in_and_preserves_stale_outputs(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        translation = self.write_rows(self.translated_rows(rows))
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        report = self.workspace / "qa/Test.apply.md"
        output.write_bytes(b"stale")
        report.write_text("stale-report\n", encoding="utf-8")
        result, output, report = self.apply_fixture(fixture, translation, "fallout4")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"stale")
        self.assertEqual(report.read_text(encoding="utf-8"), "stale-report\n")

    def test_fallout4_apply_rejects_empty_translation_set(self) -> None:
        fixture = self.build_fixture("fallout4")
        translation = self.write_rows([])
        result, output, _ = self.apply_fixture(
            fixture,
            translation,
            "fallout4",
            allow_experimental=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_shared_source_requires_all_occurrences_with_same_target(self) -> None:
        fixture = self.build_fixture("fallout4", "shared")
        _, rows = self.export_fixture(fixture, "fallout4")
        translated = self.translated_rows(rows)
        self.assertEqual(len(translated), 2)

        partial = self.write_rows(translated[:1])
        partial_result, output, _ = self.apply_fixture(
            fixture, partial, "fallout4", allow_experimental=True
        )
        self.assertNotEqual(partial_result.returncode, 0)
        self.assertFalse(output.exists())

        complete = self.write_rows(translated)
        complete_result, output, report = self.apply_fixture(
            fixture, complete, "fallout4", allow_experimental=True
        )
        self.assertEqual(complete_result.returncode, 0, complete_result.stdout + complete_result.stderr)
        self.assertTrue(output.is_file())
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("- game_id: fallout4", report_text)
        self.assertIn("- pex_category: Fallout4", report_text)
        self.assertIn("- writeback_status: experimental", report_text)
        self.assertIn("- experimental_opt_in: True", report_text)
        self.assertRegex(report_text, r"(?m)^- Input SHA256: [0-9A-F]{64}$")
        self.assertRegex(report_text, r"(?m)^- Translation JSONL SHA256: [0-9A-F]{64}$")
        self.assertRegex(report_text, r"(?m)^- Output SHA256: [0-9A-F]{64}$")
        self.assertIn("- Input functions: 4", report_text)
        self.assertIn("- Output functions: 4", report_text)
        self.assertIn("- Input instructions: 4", report_text)
        self.assertIn("- Output instructions: 4", report_text)
        self.assertIn("- Structure preserved: True", report_text)

        output_export, output_rows = self.export_fixture(output, "fallout4")
        self.assertEqual(output_export.returncode, 0, output_export.stdout + output_export.stderr)
        self.assertEqual(sum(row.get("Source") == "共享可见文本" for row in output_rows), 2)

    def test_production_apply_passes_fresh_read_only_verification(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        translation = self.write_rows(self.translated_rows(rows))
        apply_result, output, apply_report = self.apply_fixture(
            fixture,
            translation,
            "fallout4",
            allow_experimental=True,
        )
        self.assertEqual(apply_result.returncode, 0, apply_result.stdout + apply_result.stderr)

        fresh_result, fresh_report = self.verify_fixture(fixture, output, translation)
        self.assertEqual(fresh_result.returncode, 0, fresh_result.stdout + fresh_result.stderr)
        self.assertIn("- Verification passed: True", fresh_report.read_text(encoding="utf-8"))

        python_code, python_report = self.run_python_verifier(
            fixture, output, translation, apply_report
        )
        self.assertEqual(python_code, 0, python_report.read_text(encoding="utf-8"))
        self.assertIn("Independent PEX verification passed", python_report.read_text(encoding="utf-8"))

    def test_final_binary_review_collects_two_pex_files_without_losing_game_context(self) -> None:
        source_root = self.workspace / "work/extracted_mods/TestMod"
        final_mod = self.workspace / "out/TestMod/汉化产出/final_mod"
        final_scripts = final_mod / "Scripts"
        final_scripts.mkdir(parents=True, exist_ok=True)
        for name in ("First.pex", "Second.pex"):
            original = source_root / "Scripts" / name
            final = final_scripts / name
            self.build_fixture("fallout4", path=original)
            self.build_fixture("fallout4", path=final)

        game_context = binary_review.load_game_profile("fallout4")
        def direct_export(
            root: Path,
            pex_path: Path,
            output_rel: str,
            report_rel: str,
            context,
        ) -> subprocess.CompletedProcess[str]:
            return self.run_adapter(
                "export",
                "--game",
                context.game_id,
                "--project-root",
                str(root),
                "--input-pex",
                str(pex_path),
                "--output-jsonl",
                str(root / output_rel),
                "--report",
                str(root / report_rel),
            )

        with mock.patch.object(binary_review, "run_pex_export", side_effect=direct_export):
            pex_count, items, failures = binary_review.collect_pex_items(
                self.workspace,
                source_root,
                final_mod,
                "TestMod",
                set(),
                game_context,
            )

        self.assertEqual(pex_count, 2)
        self.assertEqual(failures, [])
        self.assertEqual({item.File for item in items}, {"Scripts\\First.pex", "Scripts\\Second.pex"})
        packet = self.workspace / "qa/TestMod.pex.final_binary_review_packet.md"
        review_items = self.workspace / "qa/TestMod.pex.final_binary_review_items.jsonl"
        binary_review.write_reports(
            self.workspace,
            "TestMod",
            source_root,
            final_mod,
            packet,
            review_items,
            0,
            pex_count,
            items,
            failures,
            game_context,
        )
        packet_text = packet.read_text(encoding="utf-8")
        self.assertIn("First.pex", packet_text)
        self.assertIn("Second.pex", packet_text)

    def test_fresh_verify_rejects_header_flags_and_non_string_metadata_changes(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        translation = self.write_rows(self.translated_rows(rows, "Target visible text"))

        for variant in ("tamper-header", "tamper-flags", "tamper-non-string"):
            with self.subTest(variant=variant):
                output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
                self.build_fixture("fallout4", variant, output)
                result, report = self.verify_fixture(fixture, output, translation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "PEX invariant metadata changed",
                    report.read_text(encoding="utf-8"),
                )

    def test_direct_apply_rejects_non_md_collision_and_hardlink_aliases_before_io(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        valid_translation = self.write_rows(self.translated_rows(rows))
        output = self.workspace / "out/TestMod/tool_outputs/Scripts/Test.pex"
        output.write_bytes(b"stale-output")
        original_bytes = fixture.read_bytes()
        translation_bytes = valid_translation.read_bytes()

        non_md_report = self.workspace / "qa/Test.apply.txt"
        non_md_report.write_bytes(b"stale-report")
        result = self.run_adapter(
            "apply",
            "--game", "fallout4",
            "--project-root", str(self.workspace),
            "--input-pex", str(fixture),
            "--translation-jsonl", str(valid_translation),
            "--output-pex", str(output),
            "--report", str(non_md_report),
            "--allow-experimental-writeback",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.read_bytes(), original_bytes)
        self.assertEqual(valid_translation.read_bytes(), translation_bytes)
        self.assertEqual(output.read_bytes(), b"stale-output")
        self.assertEqual(non_md_report.read_bytes(), b"stale-report")

        exact_collision = self.run_adapter(
            "apply",
            "--game", "fallout4",
            "--project-root", str(self.workspace),
            "--input-pex", str(fixture),
            "--translation-jsonl", str(valid_translation),
            "--output-pex", str(output),
            "--report", str(valid_translation),
            "--allow-experimental-writeback",
        )
        self.assertNotEqual(exact_collision.returncode, 0)
        self.assertEqual(fixture.read_bytes(), original_bytes)
        self.assertEqual(valid_translation.read_bytes(), translation_bytes)
        self.assertEqual(output.read_bytes(), b"stale-output")

        for alias_kind in ("translation", "report", "output"):
            with self.subTest(alias=alias_kind):
                alias = {
                    "translation": self.workspace / "work/normalized/TestMod/InputAlias.jsonl",
                    "report": self.workspace / "qa/InputAlias.md",
                    "output": self.workspace / "out/TestMod/tool_outputs/Scripts/InputAlias.pex",
                }[alias_kind]
                alias.unlink(missing_ok=True)
                os.link(fixture, alias)
                translation_arg = alias if alias_kind == "translation" else valid_translation
                report_arg = alias if alias_kind == "report" else self.workspace / "qa/Test.alias.apply.md"
                output_arg = alias if alias_kind == "output" else output
                if report_arg != alias:
                    report_arg.write_bytes(b"alias-report")
                result = self.run_adapter(
                    "apply",
                    "--game", "fallout4",
                    "--project-root", str(self.workspace),
                    "--input-pex", str(fixture),
                    "--translation-jsonl", str(translation_arg),
                    "--output-pex", str(output_arg),
                    "--report", str(report_arg),
                    "--allow-experimental-writeback",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.read_bytes(), original_bytes)
                self.assertEqual(alias.read_bytes(), original_bytes)
                if output_arg == output:
                    self.assertEqual(output.read_bytes(), b"stale-output")

    def test_fake_apply_report_cannot_hide_occurrence_or_metadata_tampering(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        translation = self.write_rows(self.translated_rows(rows))

        for variant in ("occurrence", "metadata"):
            with self.subTest(variant=variant):
                apply_result, output, _ = self.apply_fixture(
                    fixture,
                    translation,
                    "fallout4",
                    allow_experimental=True,
                )
                self.assertEqual(
                    apply_result.returncode,
                    0,
                    apply_result.stdout + apply_result.stderr,
                )
                output_bytes = output.read_bytes()
                if variant == "occurrence":
                    source_bytes = "共享可见文本".encode("utf-8")
                    target_bytes = "错误目标文本".encode("utf-8")
                else:
                    source_bytes = b"Stable metadata"
                    target_bytes = b"Tamper metadata"
                self.assertEqual(len(source_bytes), len(target_bytes))
                self.assertIn(source_bytes, output_bytes)
                output.write_bytes(output_bytes.replace(source_bytes, target_bytes, 1))
                fake_apply_report = self.write_fake_apply_report(fixture, output, translation)
                code, report = self.run_python_verifier(
                    fixture, output, translation, fake_apply_report
                )
                self.assertEqual(code, 1)
                report_text = report.read_text(encoding="utf-8")
                self.assertIn("Independent PEX verification failed", report_text)
                independent_reports = [self.workspace / "qa/Test.fresh-verify.md"]
                self.assertTrue(independent_reports[0].is_file(), report_text)
                self.assertTrue(
                    any(
                        "Unexpected PEX string change" in item.read_text(encoding="utf-8")
                        or "metadata strings changed" in item.read_text(encoding="utf-8")
                        or "PEX invariant metadata changed" in item.read_text(encoding="utf-8")
                        for item in independent_reports
                    ),
                    independent_reports[0].read_text(encoding="utf-8"),
                )

    def test_shared_source_rejects_conflicting_targets(self) -> None:
        fixture = self.build_fixture("fallout4", "shared")
        _, rows = self.export_fixture(fixture, "fallout4")
        translated = self.translated_rows(rows)
        translated[1]["Result"] = "另一个目标"
        result, output, _ = self.apply_fixture(
            fixture,
            self.write_rows(translated),
            "fallout4",
            allow_experimental=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_fallout4_rejects_source_and_occurrence_identity_drift(self) -> None:
        fixture = self.build_fixture("fallout4")
        _, rows = self.export_fixture(fixture, "fallout4")
        translated = self.translated_rows(rows)
        for field, value in (("Source", "Drifted source"), ("instruction_index", 99)):
            with self.subTest(field=field):
                drifted = [{**translated[0], field: value}]
                result, output, _ = self.apply_fixture(
                    fixture,
                    self.write_rows(drifted),
                    "fallout4",
                    allow_experimental=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_metadata_and_cmp_shared_references_fail_closed(self) -> None:
        for variant in ("metadata", "property-metadata", "debug", "identifier", "cmp"):
            with self.subTest(variant=variant):
                fixture = self.build_fixture("fallout4", variant)
                _, rows = self.export_fixture(fixture, "fallout4")
                result, output, _ = self.apply_fixture(
                    fixture,
                    self.write_rows(self.translated_rows(rows)),
                    "fallout4",
                    allow_experimental=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_skyrim_v1_source_based_apply_remains_compatible(self) -> None:
        fixture = self.build_fixture("skyrim-se")
        translation = self.write_rows(
            [
                {
                    "ModName": "Test.pex",
                    "Source": "Shared visible text",
                    "Result": "共享可见文本",
                    "risk": "candidate",
                }
            ]
        )
        result, output, _ = self.apply_fixture(fixture, translation, "skyrim-se")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
