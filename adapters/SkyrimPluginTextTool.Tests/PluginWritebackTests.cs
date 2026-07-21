using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Plugins.Records;
using Mutagen.Bethesda.Skyrim;
using Xunit;
using FalloutWeapon = Mutagen.Bethesda.Fallout4.Weapon;

namespace SkyrimPluginTextTool.Tests;

public sealed class PluginWritebackTests : IDisposable
{
    private readonly string _root;

    public PluginWritebackTests()
    {
        _root = Path.Combine(Directory.GetCurrentDirectory(), ".tmp", "task3-csharp-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_root);
    }

    [Fact]
    public void Fallout4SuccessfulWritebackReparsesAndCommitsTranslatedName()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated Laser Rifle"));
        var original = Fallout4Mod.CreateFromBinary(input, Fallout4Release.Fallout4);
        var inputHash = Sha256(input);

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        var reparsed = Fallout4Mod.CreateFromBinary(output, Fallout4Release.Fallout4);
        Assert.Equal("Translated Laser Rifle", reparsed.Weapons.Single().Name?.String);
        Assert.Equal("FixtureWeapon", reparsed.Weapons.Single().EditorID);
        Assert.Equal(original.GetRecordCount(), reparsed.GetRecordCount());
        Assert.Equal(FormKeys(original), FormKeys(reparsed));
        Assert.Equal(Masters(original), Masters(reparsed));
        Assert.Equal(inputHash, Sha256(input));
        var reportText = File.ReadAllText(report);
        Assert.Contains("Reparse succeeded: True", reportText);
        Assert.Matches(@"Input record count: [1-9][0-9]*", reportText);
        Assert.Matches(@"Output record count: [1-9][0-9]*", reportText);
        Assert.Contains("Record count preserved: True", reportText);
        Assert.Contains("Input FormKeys: ", reportText);
        Assert.Contains("Output FormKeys: ", reportText);
        Assert.Contains("FormKey set preserved: True", reportText);
        Assert.Contains("Input masters: ", reportText);
        Assert.Contains("Output masters: ", reportText);
        Assert.Contains("Masters preserved: True", reportText);
        Assert.Contains("Structural validation succeeded: True", reportText);
        Assert.Matches(@"Output SHA256: [0-9A-F]{64}", reportText);
        Assert.Matches(@"Input SHA256: [0-9A-F]{64}", reportText);
        Assert.Matches(@"Translation SHA256: [0-9A-F]{64}", reportText);
        Assert.Contains("Parsed structural and payload invariant verified: True", reportText);
        Assert.Contains("Allowed header changes: GRUP header bytes 4..7", reportText);
        Assert.Contains("Reparse target: final-output", reportText);
        Assert.Contains("Structural validation target: final-output", reportText);
        AssertReportStatus(report, "ready");
    }

    [Fact]
    public void Fallout4ExportUsesMutagenAndWritesV2Identity()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ExportFixture.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "ExportFixture.jsonl");
        var report = PathFor("qa", "ExportFixture.export.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        var rows = File.ReadAllLines(output)
            .Where(static line => !string.IsNullOrWhiteSpace(line))
            .Select(static line => JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!)
            .ToArray();
        var row = Assert.Single(rows, item => item["record_type"].GetString() == "WEAP");
        Assert.Equal(2, row["schema_version"].GetInt32());
        Assert.Equal("fallout4", row["game_id"].GetString());
        Assert.Equal("ExportFixture.esp", row["plugin"].GetString());
        Assert.Equal(weapon.FormKey.ID.ToString("X8"), row["form_id"].GetString());
        Assert.Equal("Name", row["field_path"].GetString());
        Assert.Equal("FULL", row["subrecord_type"].GetString());
        Assert.Equal("Laser Rifle", row["source"].GetString());
        Assert.Equal("candidate", row["risk"].GetString());
        Assert.Equal("supported", row["writeback"].GetString());
        AssertOrdinarySchemaV2Identity(row);
        var reportText = File.ReadAllText(report);
        Assert.Contains("Operation: export", reportText);
        Assert.Contains("plugin_adapter: mutagen-bethesda-plugin", reportText);
        Assert.Contains("support_level: read_only", reportText);
        Assert.Contains("plugin_text_capability_level: read_only", reportText);
        Assert.Matches(@"Input SHA256: [0-9A-F]{64}", reportText);
        Assert.Matches(@"Output JSONL SHA256: [0-9A-F]{64}", reportText);
        Assert.Contains("- Master-style context: <none>", reportText);
        AssertReportStatus(report, "ready");
    }

    [Fact]
    public void SkyrimExportUsesControlledFieldContract()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimExport.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "SkyrimExport.jsonl");
        var report = PathFor("qa", "SkyrimExport.export.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");

        var result = RunExportAdapter("skyrim-se", input, output, report);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        var row = Assert.Single(
            File.ReadAllLines(output)
                .Where(static line => !string.IsNullOrWhiteSpace(line))
                .Select(static line => JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!));
        Assert.Equal(2, row["schema_version"].GetInt32());
        Assert.Equal("skyrim-se", row["game_id"].GetString());
        Assert.Equal("SkyrimExport.esp", row["plugin"].GetString());
        Assert.Equal(weapon.FormKey.ID.ToString("X8"), row["form_id"].GetString());
        Assert.Equal("WEAP", row["record_type"].GetString());
        Assert.Equal("Name", row["field_path"].GetString());
        Assert.Equal("FULL", row["subrecord_type"].GetString());
        Assert.Equal("Steel Sword", row["source"].GetString());
        Assert.Equal("candidate", row["risk"].GetString());
        Assert.Equal("supported", row["writeback"].GetString());
        AssertOrdinarySchemaV2Identity(row);
    }

    [Fact]
    public void Fallout4EslExportAllowsResolvableFormIds()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ExportFixture.esl");
        var output = PathFor("source", "plugin_exports", "TestMod", "ExportFixture.jsonl");
        var report = PathFor("qa", "ExportFixture.export.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        var row = Assert.Single(
            File.ReadAllLines(output)
                .Where(static line => !string.IsNullOrWhiteSpace(line))
                .Select(static line => JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!));
        Assert.Equal("ExportFixture.esl", row["plugin"].GetString());
        Assert.Equal(weapon.FormKey.ID.ToString("X8"), row["form_id"].GetString());
        Assert.Equal("WEAP", row["record_type"].GetString());
        Assert.Equal("Name", row["field_path"].GetString());
        Assert.Equal("FULL", row["subrecord_type"].GetString());
        var parsed = Fallout4Mod.CreateFromBinary(input, Fallout4Release.Fallout4);
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: true,
            lightByHeader: parsed.ModHeader.Flags.HasFlag(Fallout4ModHeader.HeaderFlag.Small),
            containsUnsupportedLightFormIds: false);
    }

    [Fact]
    public void Fallout4ExportSkipsEmptySupportedSubrecord()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "EmptyDescription.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "EmptyDescription.jsonl");
        var report = PathFor("qa", "EmptyDescription.export.md");
        CreateFallout4SpellWithEmptyDescription(input);

        var result = RunExportAdapter("fallout4", input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        var row = ReadSingleRow(output);
        Assert.Equal("FULL", row["subrecord_type"].GetString());
        Assert.Equal("Visible Spell", row["source"].GetString());
    }

    [Fact]
    public void Fallout4ExportRejectsRawLoadOrderFormIdWithoutOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LightFormId.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "LightFormId.jsonl");
        var report = PathFor("qa", "LightFormId.export.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        MutateRecordFormId(input, "WEAP", weapon.FormKey.ID, 0xFE000800);

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains(
            "raw 0xFE/load-order FormID cannot authorize",
            File.ReadAllText(report));
        AssertReportStatus(report, "blocked");
        AssertReportTraits(
            report,
            localized: null,
            lightByExtension: false,
            lightByHeader: null,
            containsUnsupportedLightFormIds: null);
    }

    [Fact]
    public void Fallout4ExportRejectsUnsupportedRecordWithRawLoadOrderFormIdBeforeWritingRows()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "UnsupportedLightFormId.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "UnsupportedLightFormId.jsonl");
        var report = PathFor("qa", "UnsupportedLightFormId.export.md");
        var formKey = CreateFallout4PluginWithUnsupportedRecord(input);
        MutateRecordFormId(input, "STAT", formKey.ID, 0xFE000800);

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains(
            "raw 0xFE/load-order FormID cannot authorize",
            File.ReadAllText(report));
        AssertReportTraits(
            report,
            localized: null,
            lightByExtension: false,
            lightByHeader: null,
            containsUnsupportedLightFormIds: null);
    }

    [Fact]
    public void Fallout4ZeroSubrecordLightFormIdBlocksExportAndApplyWithoutOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ZeroSubrecordLightFormId.esp");
        var exportOutput = PathFor("source", "plugin_exports", "TestMod", "ZeroSubrecordLightFormId.jsonl");
        var exportReport = PathFor("qa", "ZeroSubrecordLightFormId.export.md");
        var applyOutput = PathFor("out", "TestMod", "tool_outputs", "ZeroSubrecordLightFormId.esp");
        var applyRows = PathFor("translated", "plugin_exports", "TestMod", "ZeroSubrecordLightFormId.zh.jsonl");
        var applyReport = PathFor("qa", "ZeroSubrecordLightFormId.write.md");
        var (weapon, emptyRecord) = CreateFallout4PluginWithZeroSubrecordRecord(input);
        AssertRecordDataSize(input, "STAT", emptyRecord.ID, 0);
        MutateRecordFormId(input, "STAT", emptyRecord.ID, 0xFE000800);
        File.WriteAllText(exportOutput, "stale-export-output");
        File.WriteAllText(applyOutput, "stale-apply-output");
        WriteRows(
            applyRows,
            Row(
                "fallout4",
                "ZeroSubrecordLightFormId.esp",
                "WEAP",
                weapon.FormKey.ID,
                "Name",
                "FULL",
                "Laser Rifle",
                "Translated Laser Rifle"));

        var exported = RunExportAdapter(
            "fallout4",
            input,
            exportOutput,
            exportReport,
            capabilityLevel: "read_only");

        Assert.Equal(2, exported.ExitCode);
        Assert.False(File.Exists(exportOutput));
        Assert.Contains(
            "raw 0xFE/load-order FormID cannot authorize",
            File.ReadAllText(exportReport));
        AssertReportTraits(
            exportReport,
            localized: null,
            lightByExtension: false,
            lightByHeader: null,
            containsUnsupportedLightFormIds: null);

        var applied = RunAdapter("fallout4", input, applyRows, applyOutput, applyReport);

        Assert.NotEqual(0, applied.ExitCode);
        Assert.False(File.Exists(applyOutput));
        Assert.Contains(
            "raw 0xFE/load-order FormID cannot authorize",
            applied.Stdout + applied.Stderr + ReportText(applyReport));
    }

    [Fact]
    public void Fallout4LocalizedExportReportsTraitsAndFailsClosed()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LocalizedFixture.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "LocalizedFixture.jsonl");
        var report = PathFor("qa", "LocalizedFixture.export.md");
        CreateFallout4Plugin(
            input,
            "Laser Rifle",
            Fallout4ModHeader.HeaderFlag.Localized);

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        AssertReportTraits(
            report,
            localized: true,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: false);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void OrdinaryFullPluginDoesNotBuildMasterStyleContext(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "OrdinaryFull.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "OrdinaryFull.jsonl");
        var report = PathFor("qa", $"OrdinaryFull.{game}.export.md");
        CreateGamePlugin(game, input, "Visible Name");

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        var context = ExpectedMasterStyleContext(input);
        Assert.False(File.Exists(context));
        Assert.Contains("- Master-style context: <none>", File.ReadAllText(report), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void ApplyBlocksWhenEspMasterStyleCannotBeConfirmed(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "UnknownMasterPatch.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "UnknownMasterPatch.jsonl");
        var exportReport = PathFor("qa", $"UnknownMasterPatch.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "UnknownMasterPatch.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "UnknownMasterPatch.esp");
        var applyReport = PathFor("qa", $"UnknownMasterPatch.{game}.apply.md");
        CreateGamePluginWithMasters(game, input, "Visible Name", 0x800, "SomeMaster.esp");

        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.Equal(2, applied.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_unknown", ReportText(applyReport), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void ApplyUsesWorkspaceHeaderForSmallFlaggedEspMaster(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "FlaggedMasterPatch.esp");
        var master = Path.Combine(Path.GetDirectoryName(input)!, "FlaggedMaster.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "FlaggedMasterPatch.jsonl");
        var exportReport = PathFor("qa", $"FlaggedMasterPatch.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "FlaggedMasterPatch.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "FlaggedMasterPatch.esp");
        var applyReport = PathFor("qa", $"FlaggedMasterPatch.{game}.apply.md");
        CreateGamePluginWithMasters(game, input, "Visible Name", 0x800, "FlaggedMaster.esp");
        CreateGamePlugin(game, master, "Master Name", lightByHeader: true);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        var context = ExpectedMasterStyleContext(input);
        Assert.Contains("\"small_flag\": true", File.ReadAllText(context), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void ApplyRejectsStaleMasterStyleManifestHash(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "StaleManifestPatch.esp");
        var inspectedMaster = PathFor("work", "master_context", "TestMod", "SomeMaster.esp");
        var manifest = PathFor("work", "plugin_context", "TestMod", "StaleManifestPatch.master-styles.json");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "StaleManifestPatch.jsonl");
        var exportReport = PathFor("qa", $"StaleManifestPatch.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "StaleManifestPatch.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "StaleManifestPatch.esp");
        var applyReport = PathFor("qa", $"StaleManifestPatch.{game}.apply.md");
        CreateGamePluginWithMasters(game, input, "Visible Name", 0x800, "SomeMaster.esp");
        CreateGamePlugin(game, inspectedMaster, "Master Name", lightByHeader: true);
        WriteMasterStyleManifest(
            manifest,
            game,
            "StaleManifestPatch.esp",
            ("SomeMaster.esp", "light", inspectedMaster));
        File.AppendAllText(inspectedMaster, "stale");
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(
            game,
            input,
            translatedRows,
            output,
            applyReport,
            masterStyleManifest: manifest);

        Assert.Equal(2, applied.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_evidence_stale", ReportText(applyReport), StringComparison.Ordinal);
    }

    [Fact]
    public void MasterStyleContextsUseTheFullInputIdentity()
    {
        var first = PathFor("work", "extracted_mods", "TestMod", "A", "Same.esp");
        var second = PathFor("work", "extracted_mods", "TestMod", "B", "Same.esp");
        var modKey = ModKey.FromNameAndExtension("Same.esp");

        var firstContext = PluginMasterStyleContext.ContextPathFor(_root, first, modKey);
        var secondContext = PluginMasterStyleContext.ContextPathFor(_root, second, modKey);

        Assert.NotEqual(firstContext, secondContext);
        Assert.StartsWith(
            PathFor("work", "plugin_context", "resolved"),
            firstContext,
            StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void MalformedMasterStyleManifestUsesStableConflictCode()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MalformedPatch.esp");
        var manifest = PathFor("work", "plugin_context", "TestMod", "MalformedPatch.master-styles.json");
        var output = PathFor("source", "plugin_exports", "TestMod", "MalformedPatch.jsonl");
        var report = PathFor("qa", "MalformedPatch.export.md");
        CreateGamePluginWithMasters("skyrim-se", input, "Visible Name", 0x800, "SomeMaster.esm");
        Directory.CreateDirectory(Path.GetDirectoryName(manifest)!);
        File.WriteAllText(manifest, "{not-json");

        var result = RunExportAdapter(
            "skyrim-se",
            input,
            output,
            report,
            masterStyleManifest: manifest);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_conflict", ReportText(report), StringComparison.Ordinal);
    }

    [Fact]
    public void InvalidUtf8MasterStyleManifestUsesStableConflictCode()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "InvalidUtf8Patch.esp");
        var manifest = PathFor("work", "plugin_context", "TestMod", "InvalidUtf8Patch.master-styles.json");
        var output = PathFor("source", "plugin_exports", "TestMod", "InvalidUtf8Patch.jsonl");
        var report = PathFor("qa", "InvalidUtf8Patch.export.md");
        CreateGamePluginWithMasters("skyrim-se", input, "Visible Name", 0x800, "SomeMaster.esm");
        Directory.CreateDirectory(Path.GetDirectoryName(manifest)!);
        File.WriteAllBytes(
            manifest,
            "{\"schema_version\":2,\"note\":\""u8.ToArray().Concat(new byte[] { 0xFF }).ToArray());

        var result = RunExportAdapter(
            "skyrim-se",
            input,
            output,
            report,
            masterStyleManifest: manifest);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_conflict", ReportText(report), StringComparison.Ordinal);
        Assert.Contains("not valid UTF-8", ReportText(report), StringComparison.Ordinal);
    }

    [Fact]
    public void InvalidInspectedMasterHeaderUsesStableConflictCode()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "InvalidMasterPatch.esp");
        var inspectedMaster = PathFor("work", "master_context", "TestMod", "SomeMaster.esm");
        var manifest = PathFor("work", "plugin_context", "TestMod", "InvalidMasterPatch.master-styles.json");
        var output = PathFor("source", "plugin_exports", "TestMod", "InvalidMasterPatch.jsonl");
        var report = PathFor("qa", "InvalidMasterPatch.export.md");
        CreateGamePluginWithMasters("skyrim-se", input, "Visible Name", 0x800, "SomeMaster.esm");
        Directory.CreateDirectory(Path.GetDirectoryName(inspectedMaster)!);
        File.WriteAllBytes(inspectedMaster, "not-a-tes4-plugin"u8.ToArray());
        Directory.CreateDirectory(Path.GetDirectoryName(manifest)!);
        File.WriteAllText(
            manifest,
            JsonSerializer.Serialize(new
            {
                schema_version = 2,
                game_id = "skyrim-se",
                plugin = "InvalidMasterPatch.esp",
                masters = new[]
                {
                    new
                    {
                        mod_key = "SomeMaster.esm",
                        master_style = "full",
                        inspected_path = "work/master_context/TestMod/SomeMaster.esm",
                        inspected_sha256 = Sha256(inspectedMaster),
                        small_flag = false,
                    },
                },
            }));

        var result = RunExportAdapter(
            "skyrim-se",
            input,
            output,
            report,
            masterStyleManifest: manifest);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_conflict", ReportText(report), StringComparison.Ordinal);
    }

    [Fact]
    public void ManifestEvidenceRejectsAReparsePointParent()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LinkedMasterPatch.esp");
        var protectedRoot = PathFor("mod", "protected-master");
        var inspectedMaster = Path.Combine(protectedRoot, "SomeMaster.esm");
        var link = PathFor("work", "master_context", "linked");
        var manifest = PathFor("work", "plugin_context", "TestMod", "LinkedMasterPatch.master-styles.json");
        var output = PathFor("source", "plugin_exports", "TestMod", "LinkedMasterPatch.jsonl");
        var report = PathFor("qa", "LinkedMasterPatch.export.md");
        CreateGamePluginWithMasters("skyrim-se", input, "Visible Name", 0x800, "SomeMaster.esm");
        CreateGamePlugin("skyrim-se", inspectedMaster, "Master Name");
        Directory.CreateDirectory(Path.GetDirectoryName(link)!);
        try
        {
            Directory.CreateSymbolicLink(link, protectedRoot);
        }
        catch (Exception exc) when (exc is UnauthorizedAccessException or IOException)
        {
            throw Xunit.Sdk.SkipException.ForSkip(
                $"Directory symbolic links are unavailable: {exc.Message}");
        }
        Directory.CreateDirectory(Path.GetDirectoryName(manifest)!);
        File.WriteAllText(
            manifest,
            JsonSerializer.Serialize(new
            {
                schema_version = 2,
                game_id = "skyrim-se",
                plugin = "LinkedMasterPatch.esp",
                masters = new[]
                {
                    new
                    {
                        mod_key = "SomeMaster.esm",
                        master_style = "full",
                        inspected_path = "work/master_context/linked/SomeMaster.esm",
                        inspected_sha256 = Sha256(inspectedMaster),
                        small_flag = false,
                    },
                },
            }));

        var result = RunExportAdapter(
            "skyrim-se",
            input,
            output,
            report,
            masterStyleManifest: manifest);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_evidence_stale", ReportText(report), StringComparison.Ordinal);
    }

    [Fact]
    public void ManifestEvidenceRejectsAHardlinkedMaster()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw Xunit.Sdk.SkipException.ForSkip("Windows hardlink identity is required by this adapter.");
        }
        var input = PathFor("work", "extracted_mods", "TestMod", "HardlinkedMasterPatch.esp");
        var originalMaster = PathFor("mod", "protected-master", "SomeMaster.esm");
        var inspectedMaster = PathFor("work", "master_context", "TestMod", "SomeMaster.esm");
        var manifest = PathFor("work", "plugin_context", "TestMod", "HardlinkedMasterPatch.master-styles.json");
        CreateGamePluginWithMasters("skyrim-se", input, "Visible Name", 0x800, "SomeMaster.esm");
        CreateGamePlugin("skyrim-se", originalMaster, "Master Name");
        if (!CreateHardLink(inspectedMaster, originalMaster, IntPtr.Zero))
        {
            throw Xunit.Sdk.SkipException.ForSkip(
                $"Hardlinks are unavailable: Win32 error {Marshal.GetLastWin32Error()}");
        }
        WriteMasterStyleManifest(
            manifest,
            "skyrim-se",
            "HardlinkedMasterPatch.esp",
            ("SomeMaster.esm", "full", inspectedMaster));

        var error = Assert.Throws<InvalidDataException>(() => PluginMasterStyleContext.Resolve(
            _root,
            input,
            "skyrim-se",
            manifest,
            requireCompleteMap: true));

        Assert.Contains("master_style_evidence_stale", error.Message, StringComparison.Ordinal);
        Assert.Contains("multiple hardlinks", error.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LocalizedInventoryExportsNormalPluginReferences(string game)
    {
        var input = PathFor(
            "work",
            "extracted_mods",
            "TestMod",
            $"LocalizedNormal.{(game == "fallout4" ? "esp" : "esm")}");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            $"LocalizedNormal.{game}.references.jsonl");
        var report = PathFor("qa", $"LocalizedNormal.{game}.inventory.md");
        var formKey = CreateLocalizedGamePlugin(game, input, "Visible Name");

        var result = RunLocalizedInventoryAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        var row = ReadSingleRow(output);
        Assert.Equal(1, row["schema_version"].GetInt32());
        Assert.Equal(game, row["game_id"].GetString());
        Assert.Equal(Path.GetFileName(input), row["plugin"].GetString());
        Assert.True(row["localized_flag"].GetBoolean());
        Assert.Equal(Path.GetFileName(input), row["owner_mod_key"].GetString());
        Assert.Equal(formKey.ID, row["local_id"].GetUInt32());
        Assert.Equal("full", row["master_style"].GetString());
        Assert.Equal("strings", row["table_type"].GetString());
        Assert.True(row["string_id"].GetUInt32() > 0);
        Assert.Contains("- Operation: localized_inventory", File.ReadAllText(report));
        Assert.Contains("- Table type counts: strings=1", File.ReadAllText(report));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LocalizedInventorySkipsZeroStringIdSentinel(string game)
    {
        var extension = game == "fallout4" ? "esp" : "esm";
        var input = PathFor(
            "work",
            "extracted_mods",
            "TestMod",
            $"LocalizedZeroId.{extension}");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            $"LocalizedZeroId.{game}.references.jsonl");
        var report = PathFor("qa", $"LocalizedZeroId.{game}.inventory.md");
        CreateLocalizedGamePlugin(game, input, "Visible Name");
        SetFirstSubrecordUInt32(input, "WEAP", "FULL", 0);

        var result = RunLocalizedInventoryAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        Assert.DoesNotContain(
            File.ReadAllLines(output),
            static line => !string.IsNullOrWhiteSpace(line));
        Assert.Contains("- Referenced rows: 0", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimLocalizedInventoryMapsAllStringTableTypes()
    {
        var input = PathFor(
            "work",
            "extracted_mods",
            "TestMod",
            "LocalizedTableTypes.esp");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            "LocalizedTableTypes.references.jsonl");
        var report = PathFor("qa", "LocalizedTableTypes.inventory.md");
        CreateSkyrimLocalizedTableTypePlugin(input);

        var result = RunLocalizedInventoryAdapter("skyrim-se", input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        var rows = File.ReadAllLines(output)
            .Where(static line => !string.IsNullOrWhiteSpace(line))
            .Select(static line =>
                JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!)
            .ToArray();
        Assert.Contains(rows, row => row["table_type"].GetString() == "strings");
        Assert.Contains(rows, row => row["table_type"].GetString() == "dlstrings");
        Assert.Contains(rows, row => row["table_type"].GetString() == "ilstrings");
        Assert.All(rows, row => Assert.True(row["string_id"].GetUInt32() > 0));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LocalizedDeliveryInventoryRequiresCompleteMasterStyleEvidence(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LocalizedPatch.esp");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            $"LocalizedPatch.{game}.references.jsonl");
        var report = PathFor("qa", $"LocalizedPatch.{game}.inventory.md");
        CreateLocalizedGamePluginWithMasters(
            game,
            input,
            "Visible Name",
            0x800,
            "UnknownFlaggedMaster.esp");

        var result = RunLocalizedInventoryAdapter(
            game,
            input,
            output,
            report,
            requireCompleteMasterStyleMap: true);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_unknown", ReportText(report), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LocalizedLightInventoryUsesCanonicalFormKey(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LocalizedLight.esl");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            $"LocalizedLight.{game}.references.jsonl");
        var report = PathFor("qa", $"LocalizedLight.{game}.inventory.md");
        var formKey = CreateLocalizedGamePlugin(game, input, "Visible Name");

        var result = RunLocalizedInventoryAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "LocalizedLight.esl", formKey.ID);
        AssertLocalizedLightContext(game, input, report);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LocalizedSmallFlaggedEspInventoryUsesCanonicalFormKey(string game)
    {
        var input = PathFor(
            "work",
            "extracted_mods",
            "TestMod",
            "LocalizedFlaggedLight.esp");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            $"LocalizedFlaggedLight.{game}.references.jsonl");
        var report = PathFor("qa", $"LocalizedFlaggedLight.{game}.inventory.md");
        var formKey = CreateLocalizedGamePlugin(
            game,
            input,
            "Visible Name",
            lightByHeader: true);

        var result = RunLocalizedInventoryAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(
            ReadSingleRow(output),
            "LocalizedFlaggedLight.esp",
            formKey.ID);
        AssertLocalizedLightContext(game, input, report);
    }

    [Fact]
    public void LocalizedInventoryExposesReferencesWhenTablesAreMissing()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MissingTables.esp");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            "MissingTables.references.jsonl");
        var report = PathFor("qa", "MissingTables.inventory.md");
        CreateLocalizedGamePlugin("fallout4", input, "Missing table value");
        foreach (var table in Directory.EnumerateFiles(
                     _root,
                     "*.*strings",
                     SearchOption.AllDirectories))
        {
            File.Delete(table);
        }

        var result = RunLocalizedInventoryAdapter("fallout4", input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        Assert.True(ReadSingleRow(output)["string_id"].GetUInt32() > 0);
    }

    [Fact]
    public void LocalizedInventoryRejectsNonLocalizedPlugin()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "NotLocalized.esp");
        var output = PathFor(
            "source",
            "localized_delivery",
            "TestMod",
            "NotLocalized.references.jsonl");
        var report = PathFor("qa", "NotLocalized.inventory.md");
        CreateFallout4Plugin(input, "Visible Name");

        var result = RunLocalizedInventoryAdapter("fallout4", input, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains(
            "does not have the localized header flag",
            File.ReadAllText(report),
            StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Fallout4BlockedExportReportsCleanupFailureForExclusivelyLockedStaleOutput()
    {
        if (!OperatingSystem.IsWindows()) return;

        var input = PathFor("work", "extracted_mods", "TestMod", "LocalizedLocked.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "LocalizedLocked.jsonl");
        var report = PathFor("qa", "LocalizedLocked.export.md");
        CreateFallout4Plugin(
            input,
            "Laser Rifle",
            Fallout4ModHeader.HeaderFlag.Localized);
        File.WriteAllText(output, "stale-output");
        using var lockedOutput = new FileStream(
            output,
            FileMode.Open,
            FileAccess.ReadWrite,
            FileShare.None);

        var result = RunExportAdapter(
            "fallout4",
            input,
            output,
            report,
            capabilityLevel: "read_only");

        Assert.Equal(2, result.ExitCode);
        Assert.True(File.Exists(output));
        Assert.True(File.Exists(report));
        var reportText = File.ReadAllText(report);
        Assert.Contains("localized plugin must use", reportText, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("localized_delivery composite adapter", reportText, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("cleanup failed", reportText, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("- Output JSONL SHA256: unavailable", reportText);
        AssertReportTraits(
            report,
            localized: true,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: false);
    }

    [Fact]
    public void Fallout4Cp1252SourcePassesBinaryInvariant()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Cp1252Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Cp1252Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Cp1252Fixture.zh.jsonl");
        var report = PathFor("qa", "Cp1252Fixture.write.md");
        const string source = "Laser \u201cRifle\u201d";
        var weapon = CreateFallout4Plugin(input, source);
        var inputBytes = File.ReadAllBytes(input);
        Assert.Contains((byte)0x93, inputBytes);
        Assert.Contains((byte)0x94, inputBytes);
        WriteRows(rows, Row("fallout4", "Cp1252Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", source, "Translated Laser Rifle"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        var reportText = File.ReadAllText(report);
        Assert.Contains("Parsed structural and payload invariant verified: True", reportText);
        Assert.Contains("Reparse target: final-output", reportText);
        Assert.Contains("Structural validation target: final-output", reportText);
    }

    [Theory]
    [InlineData(0x81)]
    [InlineData(0x8D)]
    [InlineData(0x8F)]
    [InlineData(0x90)]
    [InlineData(0x9D)]
    public void VerifyMatchesExporterReplacementForUndefinedCp1252Byte(int undefinedByte)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "UndefinedCp1252.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "UndefinedCp1252.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "UndefinedCp1252.zh.jsonl");
        var report = PathFor("qa", "UndefinedCp1252.verify.md");
        const string marker = "UndefinedByteMarker";
        var weapon = CreateFallout4Plugin(input, marker);
        File.Copy(input, output);
        MutateFirstAsciiByte(input, marker, checked((byte)undefinedByte));
        MutateFirstAsciiByte(output, marker, (byte)'A');
        WriteRows(
            rows,
            Row(
                "fallout4",
                "UndefinedCp1252.esp",
                "WEAP",
                weapon.FormKey.ID,
                "Name",
                "FULL",
                "\ufffdndefinedByteMarker",
                "AndefinedByteMarker"));

        var result = RunAdapter("verify", "fallout4", input, rows, output, report);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + (File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty));
        Assert.Contains("Parsed structural and payload invariant verified: True", File.ReadAllText(report));
    }

    [Fact]
    public void VerifyRejectsTamperedNonTargetPayload()
    {
        var fixture = ApplyFalloutFixtureWithUnknownSubrecord();
        MutateAsciiPayload(fixture.Output, "FixtureWeapon", "XixtureWeapon");

        var result = RunAdapter("verify", "fallout4", fixture.Input, fixture.Rows, fixture.Output, fixture.VerifyReport);

        Assert.Equal(2, result.ExitCode);
        Assert.Contains("Parsed structural and payload invariant verified: False", File.ReadAllText(fixture.VerifyReport));
        AssertReportStatus(fixture.VerifyReport, "blocked");
    }

    [Fact]
    public void VerifyRejectsTamperedRecordFlags()
    {
        var fixture = ApplyFalloutFixtureWithUnknownSubrecord();
        MutateRecordFlags(fixture.Output, "WEAP", 0x20);

        var result = RunAdapter("verify", "fallout4", fixture.Input, fixture.Rows, fixture.Output, fixture.VerifyReport);

        Assert.Equal(2, result.ExitCode);
        Assert.Contains("record header", File.ReadAllText(fixture.VerifyReport), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Fallout4VerifyReportIncludesPluginTraits()
    {
        var fixture = ApplyFalloutFixtureWithUnknownSubrecord();

        var result = RunAdapter(
            "verify",
            "fallout4",
            fixture.Input,
            fixture.Rows,
            fixture.Output,
            fixture.VerifyReport);

        Assert.Equal(0, result.ExitCode);
        AssertReportStatus(fixture.VerifyReport, "ready");
        AssertReportTraits(
            fixture.VerifyReport,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: false);
    }

    [Fact]
    public void Fallout4VerifyRejectsZeroSubrecordRawLoadOrderFormId()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "VerifyZeroSubrecordLightFormId.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "VerifyZeroSubrecordLightFormId.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "VerifyZeroSubrecordLightFormId.zh.jsonl");
        var report = PathFor("qa", "VerifyZeroSubrecordLightFormId.verify.md");
        var (weapon, emptyRecord) = CreateFallout4PluginWithZeroSubrecordRecord(input);
        AssertRecordDataSize(input, "STAT", emptyRecord.ID, 0);
        MutateRecordFormId(input, "STAT", emptyRecord.ID, 0xFE000800);
        File.Copy(input, output);
        WriteRows(
            rows,
            Row(
                "fallout4",
                "VerifyZeroSubrecordLightFormId.esp",
                "WEAP",
                weapon.FormKey.ID,
                "Name",
                "FULL",
                "Laser Rifle",
                "Laser Rifle"));

        var result = RunAdapter("verify", "fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.Contains(
            "raw 0xFE/load-order FormID cannot authorize",
            File.ReadAllText(report));
        AssertReportStatus(report, "blocked");
    }

    [Fact]
    public void VerifyRejectsAddedUnknownSubrecord()
    {
        var fixture = ApplyFalloutFixtureWithUnknownSubrecord();
        AppendSubrecord(fixture.Output, "WEAP", "ZZZZ", [0x10, 0x20, 0x30]);

        var result = RunAdapter("verify", "fallout4", fixture.Input, fixture.Rows, fixture.Output, fixture.VerifyReport);

        Assert.Equal(2, result.ExitCode);
        Assert.Contains("subrecord", File.ReadAllText(fixture.VerifyReport), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void SourceDriftDeletesStaleOutputAndFailsClosed()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Drifted", "激光步枪"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void MasterFormIdDoesNotCollideWithSameLocalId()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Collision.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Collision.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Collision.zh.jsonl");
        var report = PathFor("qa", "Collision.write.md");
        var localWeapon = CreateFallout4PluginWithMasterAndLocalWeapon(input, "Local Weapon", 0x800);
        CreateFallout4Plugin(Path.Combine(Path.GetDirectoryName(input)!, "Master.esm"), "Master Weapon");
        WriteRows(
            rows,
            Row("fallout4", "Collision.esp", "WEAP", localWeapon.FormKey.ID, "Name", "FULL", "Local Weapon", "不应写入", rawMasterIndex: 0));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("record identity not found", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimV2FieldMismatchFailsWithoutOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimFixture.zh.jsonl");
        var report = PathFor("qa", "SkyrimFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        WriteRows(rows, Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Description", "FULL", "Steel Sword", "钢剑"));

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void SkyrimPartialApplicationNeverWritesPlugin()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimFixture.zh.jsonl");
        var report = PathFor("qa", "SkyrimFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        File.WriteAllText(output, "stale-output");
        WriteRows(
            rows,
            Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "钢剑"),
            Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Description", "DESC", "Unused", "不支持"));

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void SkyrimSparseMessageButtonWritebackUsesExportedOccurrenceIndex()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MessageFixture.esp");
        var exportRows = PathFor("source", "plugin_exports", "TestMod", "MessageFixture.jsonl");
        var exportReport = PathFor("qa", "MessageFixture.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "MessageFixture.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "MessageFixture.esp");
        var applyReport = PathFor("qa", "MessageFixture.apply.md");
        CreateSkyrimMessagePlugin(input, "Accept", "Decline", "Decide later");
        var export = RunExportAdapter("skyrim-se", input, exportRows, exportReport);
        Assert.True(
            export.ExitCode == 0,
            export.Stdout + export.Stderr + Environment.NewLine + File.ReadAllText(exportReport));
        var thirdButton = File.ReadAllLines(exportRows)
            .Where(static line => !string.IsNullOrWhiteSpace(line))
            .Select(static line => JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!)
            .Single(static row =>
                row["record_type"].GetString() == "MESG"
                && row["subrecord_type"].GetString() == "ITXT"
                && row["occurrence_index"].GetInt32() == 2)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        thirdButton["target"] = "Decide later translated";
        WriteRows(translatedRows, thirdButton);

        var result = RunAdapter("skyrim-se", input, translatedRows, output, applyReport);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + Environment.NewLine + File.ReadAllText(applyReport));
        var message = SkyrimMod.CreateFromBinary(output, SkyrimRelease.SkyrimSE).Messages.Single();
        Assert.Equal("Accept", message.MenuButtons[0].Text?.String);
        Assert.Equal("Decline", message.MenuButtons[1].Text?.String);
        Assert.Equal("Decide later translated", message.MenuButtons[2].Text?.String);
    }

    [Fact]
    public void SkyrimSparseDialogResponseWritebackUsesExportedOccurrenceIndex()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "DialogFixture.esp");
        var exportRows = PathFor("source", "plugin_exports", "TestMod", "DialogFixture.jsonl");
        var exportReport = PathFor("qa", "DialogFixture.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "DialogFixture.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "DialogFixture.esp");
        var applyReport = PathFor("qa", "DialogFixture.apply.md");
        CreateSkyrimDialogPlugin(input, "First response", "Second response", "Third response");
        var export = RunExportAdapter("skyrim-se", input, exportRows, exportReport);
        Assert.True(
            export.ExitCode == 0,
            export.Stdout + export.Stderr + Environment.NewLine + File.ReadAllText(exportReport));
        var thirdResponse = File.ReadAllLines(exportRows)
            .Where(static line => !string.IsNullOrWhiteSpace(line))
            .Select(static line => JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!)
            .Single(static row =>
                row["record_type"].GetString() == "INFO"
                && row["subrecord_type"].GetString() == "NAM1"
                && row["occurrence_index"].GetInt32() == 2)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        thirdResponse["target"] = "Third response translated";
        WriteRows(translatedRows, thirdResponse);

        var result = RunAdapter("skyrim-se", input, translatedRows, output, applyReport);

        Assert.True(
            result.ExitCode == 0,
            result.Stdout + result.Stderr + Environment.NewLine + File.ReadAllText(applyReport));
        var response = SkyrimMod.CreateFromBinary(output, SkyrimRelease.SkyrimSE)
            .DialogTopics.Records.Single().Responses.Single();
        Assert.Equal("First response", response.Responses[0].Text?.String);
        Assert.Equal("Second response", response.Responses[1].Text?.String);
        Assert.Equal("Third response translated", response.Responses[2].Text?.String);
    }

    [Fact]
    public void SkyrimRepeatedFieldWithoutOccurrenceIndexIsRejected()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MessageFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "MessageFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "MessageFixture.zh.jsonl");
        var report = PathFor("qa", "MessageFixture.apply.md");
        CreateSkyrimMessagePlugin(input, "Accept");
        var mod = SkyrimMod.CreateFromBinary(input, SkyrimRelease.SkyrimSE);
        var message = mod.Messages.Single();
        WriteRows(
            rows,
            Row(
                "skyrim-se",
                "MessageFixture.esp",
                "MESG",
                message.FormKey.ID,
                "MenuButtons[].Text",
                "ITXT",
                "Accept",
                "Translated"));

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("requires occurrence_index", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimDuplicatePhysicalTargetIsRejected()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimFixture.zh.jsonl");
        var report = PathFor("qa", "SkyrimFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        var first = Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "First");
        var second = Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Second");
        WriteRows(rows, first, second);

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("duplicate translation target", File.ReadAllText(report));
    }

    [Fact]
    public void Fallout4SchemaV1IsRejected()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        var row = Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "激光步枪");
        row["schema_version"] = 1;
        WriteRows(rows, row);

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void SkyrimSchemaV1IsRejected()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimFixture.zh.jsonl");
        var report = PathFor("qa", "SkyrimFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        var row = Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword");
        row["schema_version"] = 1;
        WriteRows(rows, row);
        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("schema_version=1", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimEslRowWithoutCanonicalIdentityIsBlocked()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimLight.esl");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimLight.esl");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimLight.zh.jsonl");
        var report = PathFor("qa", "SkyrimLight.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        WriteRows(
            rows,
            Row(
                "skyrim-se",
                "SkyrimLight.esl",
                "WEAP",
                weapon.FormKey.ID,
                "Name",
                "FULL",
                "Steel Sword",
                "Translated Sword"));

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("light-aware row requires", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimEslFlaggedEspRowWithoutCanonicalIdentityIsBlocked()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimLight.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimLight.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimLight.zh.jsonl");
        var report = PathFor("qa", "SkyrimLight.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword", lightByHeader: true);
        WriteRows(
            rows,
            Row(
                "skyrim-se",
                "SkyrimLight.esp",
                "WEAP",
                weapon.FormKey.ID,
                "Name",
                "FULL",
                "Steel Sword",
                "Translated Sword"));

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("light-aware row requires", File.ReadAllText(report));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void StandaloneEslExportCarriesCanonicalLightIdentity(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "StandaloneLight.esl");
        var output = PathFor("source", "plugin_exports", "TestMod", "StandaloneLight.jsonl");
        var report = PathFor("qa", $"StandaloneLight.{game}.export.md");
        var formKey = CreateGamePlugin(game, input, "Visible Name");

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "StandaloneLight.esl", formKey.ID);
        var context = ExpectedMasterStyleContext(input);
        Assert.True(File.Exists(context));
        Assert.Contains(
            Path.GetRelativePath(_root, context).Replace('\\', '/'),
            File.ReadAllText(report).Replace('\\', '/'),
            StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void SmallFlaggedEspExportCarriesCanonicalLightIdentity(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "FlaggedLight.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "FlaggedLight.jsonl");
        var report = PathFor("qa", $"FlaggedLight.{game}.export.md");
        var formKey = CreateGamePlugin(game, input, "Visible Name", lightByHeader: true);

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "FlaggedLight.esp", formKey.ID);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LightMasterReferenceResolvesCanonicalOwner(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "LightMasterPatch.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "LightMasterPatch.jsonl");
        var report = PathFor("qa", $"LightMasterPatch.{game}.export.md");
        CreateGamePluginOverride(
            game,
            input,
            "Visible Name",
            0x800,
            "LightMaster.esl",
            "LightMaster.esl");

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "LightMaster.esl", 0x800);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void MixedFullAndLightMasterChainResolvesLightOwner(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MixedMasterPatch.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "MixedMasterPatch.jsonl");
        var report = PathFor("qa", $"MixedMasterPatch.{game}.export.md");
        CreateGamePluginWithMasters(
            game,
            input,
            "Visible Name",
            0x801,
            "FullMaster.esm",
            "LightMaster.esl");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "FullMaster.esm"), "Full Master");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "LightMaster.esl"), "Light Master");
        MutateFirstRecordFormId(input, "WEAP", 0x01000801);

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "LightMaster.esl", 0x801);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void MultipleLightMastersUseIndependentLightIndexes(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "MultipleLightPatch.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "MultipleLightPatch.jsonl");
        var report = PathFor("qa", $"MultipleLightPatch.{game}.export.md");
        CreateGamePluginWithMasters(
            game,
            input,
            "Visible Name",
            0x802,
            "FullMaster.esm",
            "FirstLight.esl",
            "SecondLight.esp");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "FullMaster.esm"), "Full Master");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "FirstLight.esl"), "First Light");
        CreateGamePlugin(
            game,
            Path.Combine(Path.GetDirectoryName(input)!, "SecondLight.esp"),
            "Second Light",
            lightByHeader: true);
        MutateFirstRecordFormId(input, "WEAP", 0x02000802);

        var result = RunExportAdapter(game, input, output, report);

        Assert.True(result.ExitCode == 0, result.Stdout + result.Stderr + ReportText(report));
        AssertCanonicalLightIdentity(ReadSingleRow(output), "SecondLight.esp", 0x802);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void UnknownEspMasterStyleBlocksLightResolution(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "UnknownLightPatch.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "UnknownLightPatch.jsonl");
        var report = PathFor("qa", $"UnknownLightPatch.{game}.export.md");
        CreateGamePluginWithMasters(
            game,
            input,
            "Visible Name",
            0x803,
            "KnownLight.esl",
            "UnknownLight.esp");
        MutateFirstRecordFormId(input, "WEAP", 0x01000803);

        var result = RunExportAdapter(game, input, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master_style_unknown", ReportText(report), StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void ConflictingMasterStyleEvidenceBlocksResolution(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ConflictPatch.esp");
        var master = Path.Combine(Path.GetDirectoryName(input)!, "ConflictMaster.esp");
        var output = PathFor("source", "plugin_exports", "TestMod", "Conflict.jsonl");
        var report = PathFor("qa", $"Conflict.{game}.export.md");
        var manifest = PathFor("work", "plugin_context", "TestMod", "Conflict.master-styles.json");
        CreateGamePluginWithMasters(game, input, "Visible Name", 0x800, "ConflictMaster.esp");
        CreateGamePlugin(game, master, "Master Name", lightByHeader: true);
        WriteMasterStyleManifest(
            manifest,
            game,
            "ConflictPatch.esp",
            ("ConflictMaster.esp", "full", master));

        var result = RunExportAdapter(game, input, output, report, masterStyleManifest: manifest);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains(
            "master_style_conflict",
            ReportText(report),
            StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void StandaloneLightPluginApplyUsesCanonicalIdentity(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ApplyLight.esl");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "ApplyLight.jsonl");
        var exportReport = PathFor("qa", $"ApplyLight.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "ApplyLight.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "ApplyLight.esl");
        var applyReport = PathFor("qa", $"ApplyLight.{game}.apply.md");
        var formKey = CreateGamePlugin(game, input, "Visible Name");
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        Assert.Equal(
            new FormKey(ModKey.FromNameAndExtension("ApplyLight.esl"), formKey.ID),
            ReadSingleWeapon(game, output).FormKey);
        Assert.Equal("Translated Name", ReadSingleWeapon(game, output).Name);
        Assert.Contains("Current master style preserved: True", File.ReadAllText(applyReport));
        Assert.Contains("Master styles preserved: True", File.ReadAllText(applyReport));
        Assert.Contains("Small flag preserved: True", File.ReadAllText(applyReport));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void SmallFlaggedEspApplyUsesCanonicalIdentity(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ApplyFlagged.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "ApplyFlagged.jsonl");
        var exportReport = PathFor("qa", $"ApplyFlagged.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "ApplyFlagged.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "ApplyFlagged.esp");
        var applyReport = PathFor("qa", $"ApplyFlagged.{game}.apply.md");
        var formKey = CreateGamePlugin(game, input, "Visible Name", lightByHeader: true);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        Assert.Equal(
            new FormKey(ModKey.FromNameAndExtension("ApplyFlagged.esp"), formKey.ID),
            ReadSingleWeapon(game, output).FormKey);
        Assert.Equal("Translated Name", ReadSingleWeapon(game, output).Name);
        Assert.True(IsSmallFlagged(game, output));
        Assert.Contains("Input Small flag: True", File.ReadAllText(applyReport));
        Assert.Contains("Output Small flag: True", File.ReadAllText(applyReport));
        Assert.Contains("Small flag preserved: True", File.ReadAllText(applyReport));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void ApplyPreservesOriginalTes4HedrPayload(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "PreserveHeader.esl");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "PreserveHeader.jsonl");
        var exportReport = PathFor("qa", $"PreserveHeader.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "PreserveHeader.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "PreserveHeader.esl");
        var applyReport = PathFor("qa", $"PreserveHeader.{game}.apply.md");
        CreateGamePlugin(game, input, "Visible Name", lightByHeader: true);
        SetTes4Hedr(input, version: 0.95f, recordCount: 16, nextObjectId: 4071);
        var inputHedr = ReadTes4Hedr(input);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        Assert.Equal(inputHedr, ReadTes4Hedr(output));
        Assert.Equal("Translated Name", ReadSingleWeapon(game, output).Name);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void VerifyRejectsChangedTes4HedrPayload(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "VerifyHeader.esl");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "VerifyHeader.jsonl");
        var exportReport = PathFor("qa", $"VerifyHeader.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "VerifyHeader.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "VerifyHeader.esl");
        var applyReport = PathFor("qa", $"VerifyHeader.{game}.apply.md");
        var verifyReport = PathFor("qa", $"VerifyHeader.{game}.verify.md");
        CreateGamePlugin(game, input, "Visible Name", lightByHeader: true);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);
        var applied = RunAdapter(game, input, translatedRows, output, applyReport);
        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        var outputHedr = ReadTes4Hedr(output);
        SetTes4Hedr(
            output,
            BitConverter.ToSingle(outputHedr, 0),
            BitConverter.ToUInt32(outputHedr, 4) + 1,
            BitConverter.ToUInt32(outputHedr, 8));

        var verified = RunAdapter("verify", game, input, translatedRows, output, verifyReport);

        Assert.Equal(2, verified.ExitCode);
        Assert.Contains("non-target payload changed for TES4 00000000 HEDR[0]", File.ReadAllText(verifyReport));
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LightMasterOverrideApplyUsesCanonicalIdentity(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ApplyLightMaster.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "ApplyLightMaster.jsonl");
        var exportReport = PathFor("qa", $"ApplyLightMaster.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "ApplyLightMaster.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "ApplyLightMaster.esp");
        var applyReport = PathFor("qa", $"ApplyLightMaster.{game}.apply.md");
        CreateGamePluginWithMasters(game, input, "Visible Name", 0x800, "LightMaster.esl");
        MutateFirstRecordFormId(input, "WEAP", 0x00000800);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(game, input, translatedRows, output, applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        var weapon = ReadSingleWeapon(game, output);
        Assert.Equal(new FormKey(ModKey.FromNameAndExtension("LightMaster.esl"), 0x800), weapon.FormKey);
        Assert.Equal("Translated Name", weapon.Name);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void MultipleLightMasterApplySelectsCanonicalOwner(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ApplyMultipleLight.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "ApplyMultipleLight.jsonl");
        var exportReport = PathFor("qa", $"ApplyMultipleLight.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "ApplyMultipleLight.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "ApplyMultipleLight.esp");
        var applyReport = PathFor("qa", $"ApplyMultipleLight.{game}.apply.md");
        CreateGamePluginOverride(
            game,
            input,
            "Visible Name",
            0x802,
            "SecondLight.esp",
            "FullMaster.esm",
            "FirstLight.esl",
            "SecondLight.esp");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "FullMaster.esm"), "Full Master");
        CreateGamePlugin(game, Path.Combine(Path.GetDirectoryName(input)!, "FirstLight.esl"), "First Light");
        CreateGamePlugin(
            game,
            Path.Combine(Path.GetDirectoryName(input)!, "SecondLight.esp"),
            "Second Light",
            lightByHeader: true);
        var exported = RunExportAdapter(
            game,
            input,
            exportedRows,
            exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Translated Name";
        WriteRows(translatedRows, row);

        var applied = RunAdapter(
            game,
            input,
            translatedRows,
            output,
            applyReport);

        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + ReportText(applyReport));
        var weapon = ReadSingleWeapon(game, output);
        Assert.Equal(new FormKey(ModKey.FromNameAndExtension("SecondLight.esp"), 0x802), weapon.FormKey);
        Assert.Equal("Translated Name", weapon.Name);
        var reportText = File.ReadAllText(applyReport);
        Assert.Contains("FullMaster.esm|full", reportText);
        Assert.Contains("FirstLight.esl|light", reportText);
        Assert.Contains("SecondLight.esp|light", reportText);
        Assert.Contains("Master styles preserved: True", reportText);
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void VerifyRejectsChangedSmallFlag(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "VerifyFlagged.esp");
        var exportedRows = PathFor("source", "plugin_exports", "TestMod", "VerifyFlagged.jsonl");
        var exportReport = PathFor("qa", $"VerifyFlagged.{game}.export.md");
        var translatedRows = PathFor("translated", "plugin_exports", "TestMod", "VerifyFlagged.zh.jsonl");
        var output = PathFor("out", "TestMod", "tool_outputs", "VerifyFlagged.esp");
        var verifyReport = PathFor("qa", $"VerifyFlagged.{game}.verify.md");
        CreateGamePlugin(game, input, "Visible Name", lightByHeader: true);
        var exported = RunExportAdapter(game, input, exportedRows, exportReport);
        Assert.True(exported.ExitCode == 0, exported.Stdout + exported.Stderr + ReportText(exportReport));
        var row = ReadSingleRow(exportedRows)
            .ToDictionary(static item => item.Key, static item => (object)item.Value);
        row["target"] = "Visible Name";
        WriteRows(translatedRows, row);
        File.Copy(input, output);
        MutateRecordFlags(output, "TES4", 0x00000200);

        var verified = RunAdapter(
            "verify",
            game,
            input,
            translatedRows,
            output,
            verifyReport);

        Assert.Equal(2, verified.ExitCode);
        var reportText = File.ReadAllText(verifyReport);
        Assert.Contains("Current master style preserved: False", reportText);
        Assert.Contains("Small flag preserved: False", reportText);
        AssertReportStatus(verifyReport, "blocked");
    }

    [Theory]
    [InlineData("skyrim-se")]
    [InlineData("fallout4")]
    public void LightRowWithOutOfRangeCanonicalLocalIdIsRejected(string game)
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "InvalidLight.esl");
        var output = PathFor("out", "TestMod", "tool_outputs", "InvalidLight.esl");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "InvalidLight.zh.jsonl");
        var report = PathFor("qa", $"InvalidLight.{game}.write.md");
        var formKey = CreateGamePlugin(game, input, "Visible Name");
        var row = Row(
            game,
            "InvalidLight.esl",
            "WEAP",
            formKey.ID,
            "Name",
            "FULL",
            "Visible Name",
            "Translated Name");
        row["owner_mod_key"] = "InvalidLight.esl";
        row["local_id"] = 0x1000;
        row["master_style"] = "light";
        row["master_style_evidence"] = "extension:.esl";
        WriteRows(rows, row);

        var result = RunAdapter(game, input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("light local_id", ReportText(report), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void MalformedTranslationJsonlDeletesStaleApplyOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Malformed.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Malformed.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Malformed.zh.jsonl");
        var report = PathFor("qa", "Malformed.write.md");
        CreateSkyrimPlugin(input, "Steel Sword");
        File.WriteAllText(output, "stale-output");
        Directory.CreateDirectory(Path.GetDirectoryName(rows)!);
        File.WriteAllText(rows, "{not-json}\n");

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void InvalidUtf8TranslationTargetDoesNotProduceApplyOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "InvalidUtf8.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "InvalidUtf8.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "InvalidUtf8.zh.jsonl");
        var report = PathFor("qa", "InvalidUtf8.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        const string marker = "__INVALID_UTF8_TARGET__";
        var serialized = JsonSerializer.Serialize(Row(
            "skyrim-se",
            "InvalidUtf8.esp",
            "WEAP",
            weapon.FormKey.ID,
            "Name",
            "FULL",
            "Steel Sword",
            marker));
        var bytes = Encoding.UTF8.GetBytes(serialized + "\n");
        var markerBytes = Encoding.ASCII.GetBytes(marker);
        var markerOffset = bytes.AsSpan().IndexOf(markerBytes);
        Assert.True(markerOffset >= 0);
        Directory.CreateDirectory(Path.GetDirectoryName(rows)!);
        File.WriteAllBytes(
            rows,
            bytes[..markerOffset]
                .Concat(new byte[] { 0xFF })
                .Concat(bytes[(markerOffset + markerBytes.Length)..])
                .ToArray());

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void ReportWriteFailureDeletesProducedApplyOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ReportFailure.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "ReportFailure.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "ReportFailure.zh.jsonl");
        var report = PathFor("qa", "report-as-directory.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        WriteRows(rows, Row("skyrim-se", "ReportFailure.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword"));
        Directory.CreateDirectory(report);

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void VerifyPreflightFailureDoesNotDeleteTargetOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "VerifyMalformed.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "VerifyMalformed.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "VerifyMalformed.zh.jsonl");
        var report = PathFor("qa", "VerifyMalformed.verify.md");
        CreateSkyrimPlugin(input, "Steel Sword");
        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        File.Copy(input, output);
        Directory.CreateDirectory(Path.GetDirectoryName(rows)!);
        File.WriteAllText(rows, "{not-json}\n");
        var outputHash = Sha256(output);

        var result = RunAdapter("verify", "skyrim-se", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.True(File.Exists(output));
        Assert.Equal(outputHash, Sha256(output));
    }

    [Fact]
    public void UnparseableInputDeletesStaleOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Broken.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Broken.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Broken.zh.jsonl");
        var report = PathFor("qa", "Broken.write.md");
        File.WriteAllText(input, "not-a-plugin");
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "Broken.esp", "WEAP", 0x800, "Name", "FULL", "Source", "Target"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void DryRunDeletesStaleOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated"));

        var result = RunAdapter("fallout4", input, rows, output, report, dryRun: true);

        Assert.Equal(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void SchemaV2EmptySourceIsRejected()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "", "Translated"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("source", File.ReadAllText(report), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void RawLoadOrderFormIdCannotAuthorizeWriteback()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        CreateFallout4Plugin(input, "Laser Rifle");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", 0x800, "Name", "FULL", "Laser Rifle", "Translated", rawMasterIndex: 0xFE));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("master index 254", File.ReadAllText(report));
        AssertReportStatus(report, "blocked");
    }

    [Fact]
    public void Fallout4EslRowWithoutCanonicalIdentityIsBlocked()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esl");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esl");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var report = PathFor("qa", "Fixture.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "Fixture.esl", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("light-aware row requires", File.ReadAllText(report));
        var parsed = Fallout4Mod.CreateFromBinary(input, Fallout4Release.Fallout4);
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: true,
            lightByHeader: parsed.ModHeader.Flags.HasFlag(Fallout4ModHeader.HeaderFlag.Small),
            containsUnsupportedLightFormIds: false);
    }

    [Fact]
    public void Fallout4MalformedEslWritebackIsBlockedBeforeParsing()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Malformed.esl");
        var output = PathFor("out", "TestMod", "tool_outputs", "Malformed.esl");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Malformed.zh.jsonl");
        var report = PathFor("qa", "Malformed.write.md");
        File.WriteAllText(input, "not-a-plugin");
        File.WriteAllText(output, "stale-output");
        WriteRows(
            rows,
            Row("fallout4", "Malformed.esl", "WEAP", 0x800, "Name", "FULL", "Source", "Target"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains(
            "complete TES4 header",
            result.Stdout + result.Stderr + ReportText(report));
    }

    [Fact]
    public void Fallout4SmallFlaggedEspRowWithoutCanonicalIdentityIsBlocked()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SmallFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SmallFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SmallFixture.zh.jsonl");
        var report = PathFor("qa", "SmallFixture.write.md");
        var weapon = CreateFallout4Plugin(
            input,
            "Laser Rifle",
            Fallout4ModHeader.HeaderFlag.Small);
        File.WriteAllText(output, "stale-output");
        WriteRows(rows, Row("fallout4", "SmallFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated"));

        var result = RunAdapter("fallout4", input, rows, output, report);

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("light-aware row requires", File.ReadAllText(report));
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: false,
            lightByHeader: true,
            containsUnsupportedLightFormIds: false);
    }

    [Fact]
    public void RejectsGameAndMutagenReleaseMismatchBeforeReadingPaths()
    {
        var cases = new[]
        {
            (Command: "apply", Game: "fallout4", Release: "SkyrimSE"),
            (Command: "export", Game: "fallout4", Release: "SkyrimSE"),
            (Command: "verify", Game: "fallout4", Release: "SkyrimSE"),
            (Command: "apply", Game: "skyrim-se", Release: "Fallout4"),
            (Command: "export", Game: "skyrim-se", Release: "Fallout4"),
            (Command: "verify", Game: "skyrim-se", Release: "Fallout4"),
        };
        foreach (var item in cases)
        {
            var result = RunIdentityCheck(item.Command, item.Game, item.Release);
            Assert.NotEqual(0, result.ExitCode);
            var output = result.Stdout + result.Stderr;
            Assert.Contains("incompatible", output, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(item.Game, output, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(item.Release, output, StringComparison.OrdinalIgnoreCase);
        }
    }

    [Fact]
    public void IdentityPreflightFailureDeletesStaleApplyOutput()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Mismatch.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Mismatch.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Mismatch.zh.jsonl");
        var report = PathFor("qa", "Mismatch.write.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        WriteRows(rows, Row("fallout4", "Mismatch.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated"));
        File.WriteAllText(output, "stale-output");

        var result = RunAdapter(
            "apply",
            "fallout4",
            input,
            rows,
            output,
            report,
            mutagenRelease: "SkyrimSE",
            capabilityLevel: "experimental_write");

        Assert.NotEqual(0, result.ExitCode);
        Assert.False(File.Exists(output));
    }

    [Fact]
    public void ApplyRejectsUnsafeOutputRolesAndInputAliasWithoutChangingFiles()
    {
        foreach (var scenario in new[] { "input-alias", "mod-output", "work-text-output" })
        {
            var input = PathFor("work", "extracted_mods", "TestMod", $"{scenario}.esp");
            var rows = PathFor("translated", "plugin_exports", "TestMod", $"{scenario}.zh.jsonl");
            var report = PathFor("qa", $"{scenario}.write.md");
            var weapon = CreateSkyrimPlugin(input, "Steel Sword");
            WriteRows(rows, Row("skyrim-se", Path.GetFileName(input), "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword"));
            var output = scenario switch
            {
                "input-alias" => input,
                "mod-output" => PathFor("mod", "ProtectedOriginal.esp"),
                _ => PathFor("work", "Important.txt"),
            };
            if (!string.Equals(output, input, StringComparison.OrdinalIgnoreCase))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(output)!);
                File.WriteAllBytes(output, [0x50, 0x52, 0x4F, 0x54, 0x45, 0x43, 0x54]);
            }
            var inputBytes = File.ReadAllBytes(input);
            var outputBytes = File.ReadAllBytes(output);

            var result = RunAdapter("skyrim-se", input, rows, output, report);

            Assert.NotEqual(0, result.ExitCode);
            Assert.Equal(inputBytes, File.ReadAllBytes(input));
            Assert.Equal(outputBytes, File.ReadAllBytes(output));
        }
    }

    [Fact]
    public void ApplyRejectsWrongTranslationReportRolesAndOutputReportAlias()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "RoleFixture.esp");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");

        var wrongTranslation = PathFor("work", "ProtectedTranslation.jsonl");
        WriteRows(wrongTranslation, Row("skyrim-se", "RoleFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword"));
        var translationBytes = File.ReadAllBytes(wrongTranslation);
        var outputForTranslation = PathFor("out", "TestMod", "tool_outputs", "WrongTranslation.esp");
        File.WriteAllBytes(outputForTranslation, [0x53, 0x54, 0x41, 0x4C, 0x45]);
        var wrongTranslationResult = RunAdapter(
            "skyrim-se",
            input,
            wrongTranslation,
            outputForTranslation,
            PathFor("qa", "WrongTranslation.write.md"));
        Assert.NotEqual(0, wrongTranslationResult.ExitCode);
        Assert.Equal(translationBytes, File.ReadAllBytes(wrongTranslation));
        Assert.False(File.Exists(outputForTranslation));

        var rows = PathFor("translated", "plugin_exports", "TestMod", "RoleFixture.zh.jsonl");
        WriteRows(rows, Row("skyrim-se", "RoleFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword"));
        var wrongReport = PathFor("work", "ProtectedReport.md");
        File.WriteAllBytes(wrongReport, [0x52, 0x45, 0x50, 0x4F, 0x52, 0x54]);
        var reportBytes = File.ReadAllBytes(wrongReport);
        var outputForReport = PathFor("out", "TestMod", "tool_outputs", "WrongReport.esp");
        File.WriteAllBytes(outputForReport, [0x53, 0x54, 0x41, 0x4C, 0x45]);
        var wrongReportResult = RunAdapter("skyrim-se", input, rows, outputForReport, wrongReport);
        Assert.NotEqual(0, wrongReportResult.ExitCode);
        Assert.Equal(reportBytes, File.ReadAllBytes(wrongReport));
        Assert.False(File.Exists(outputForReport));

        var aliasOutput = PathFor("out", "TestMod", "tool_outputs", "OutputReportAlias.esp");
        File.WriteAllBytes(aliasOutput, [0x41, 0x4C, 0x49, 0x41, 0x53]);
        var aliasBytes = File.ReadAllBytes(aliasOutput);
        var aliasResult = RunAdapter("skyrim-se", input, rows, aliasOutput, aliasOutput);
        Assert.NotEqual(0, aliasResult.ExitCode);
        Assert.Equal(aliasBytes, File.ReadAllBytes(aliasOutput));
    }

    [Fact]
    public void ExportRejectsInputOutputAliasWithoutChangingPlugin()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "ExportAlias.esp");
        CreateFallout4Plugin(input, "Laser Rifle");
        var inputBytes = File.ReadAllBytes(input);

        var result = RunExportAdapter(
            "fallout4",
            input,
            input,
            PathFor("qa", "ExportAlias.export.md"));

        Assert.NotEqual(0, result.ExitCode);
        Assert.Equal(inputBytes, File.ReadAllBytes(input));
    }

    [Fact]
    public void ApplyRejectsReparsePointOutputWithoutChangingTarget()
    {
        var protectedRoot = PathFor("mod", "protected-target");
        Directory.CreateDirectory(protectedRoot);
        var link = PathFor("out", "linked-output");
        Directory.CreateDirectory(Path.GetDirectoryName(link)!);
        try
        {
            Directory.CreateSymbolicLink(link, protectedRoot);
        }
        catch (Exception exc) when (exc is UnauthorizedAccessException or IOException)
        {
            throw Xunit.Sdk.SkipException.ForSkip(
                $"Directory symbolic links are unavailable: {exc.Message}");
        }

        var input = PathFor("work", "extracted_mods", "TestMod", "ReparseFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "ReparseFixture.zh.jsonl");
        var report = PathFor("qa", "ReparseFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        WriteRows(rows, Row("skyrim-se", "ReparseFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword"));
        var protectedOutput = Path.Combine(protectedRoot, "ProtectedOriginal.esp");
        File.WriteAllBytes(protectedOutput, [0x50, 0x52, 0x4F, 0x54, 0x45, 0x43, 0x54]);
        var protectedBytes = File.ReadAllBytes(protectedOutput);

        var result = RunAdapter(
            "skyrim-se",
            input,
            rows,
            Path.Combine(link, "ProtectedOriginal.esp"),
            report);

        Assert.NotEqual(0, result.ExitCode);
        Assert.Equal(protectedBytes, File.ReadAllBytes(protectedOutput));
    }

    private FalloutWeapon CreateFallout4Plugin(
        string path,
        string name,
        Fallout4ModHeader.HeaderFlag headerFlags = 0)
    {
        var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(path)), Fallout4Release.Fallout4);
        mod.ModHeader.Flags |= headerFlags;
        var weapon = mod.Weapons.AddNew();
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = name;
        WriteFallout4(mod, path);
        return weapon;
    }

    private FalloutWeapon CreateFallout4PluginWithMasterAndLocalWeapon(string path, string name, uint localId)
    {
        var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(path)), Fallout4Release.Fallout4);
        ((IMod)mod).MasterReferences.Add(new MasterReference { Master = ModKey.FromNameAndExtension("Master.esm") });
        var weapon = mod.Weapons.AddNew(new FormKey(mod.ModKey, localId));
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = name;
        WriteFallout4(mod, path);
        return weapon;
    }

    private void CreateFallout4SpellWithEmptyDescription(string path)
    {
        var mod = new Fallout4Mod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            Fallout4Release.Fallout4);
        var spell = mod.Spells.AddNew();
        spell.EditorID = "FixtureSpell";
        spell.Name = "Visible Spell";
        spell.Description = string.Empty;
        WriteFallout4(mod, path);
        AppendSubrecord(path, "SPEL", "DESC", [0]);
    }

    private FormKey CreateFallout4PluginWithUnsupportedRecord(string path)
    {
        var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(path)), Fallout4Release.Fallout4);
        var record = mod.Statics.AddNew();
        record.EditorID = "FixtureStatic";
        WriteFallout4(mod, path);
        return record.FormKey;
    }

    private (FalloutWeapon Weapon, FormKey EmptyRecord) CreateFallout4PluginWithZeroSubrecordRecord(string path)
    {
        var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(path)), Fallout4Release.Fallout4);
        var weapon = mod.Weapons.AddNew();
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = "Laser Rifle";
        var emptyRecord = mod.Statics.AddNew();
        WriteFallout4(mod, path);
        ClearRecordData(path, "STAT", emptyRecord.FormKey.ID);
        return (weapon, emptyRecord.FormKey);
    }

    private Mutagen.Bethesda.Skyrim.Weapon CreateSkyrimPlugin(
        string path,
        string name,
        bool lightByHeader = false)
    {
        var mod = new SkyrimMod(ModKey.FromNameAndExtension(Path.GetFileName(path)), SkyrimRelease.SkyrimSE);
        if (lightByHeader)
        {
            mod.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Small;
        }
        var weapon = mod.Weapons.AddNew();
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite.ToPath(path).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
        return weapon;
    }

    private FormKey CreateLocalizedGamePlugin(
        string game,
        string path,
        string name,
        bool lightByHeader = false)
    {
        if (game == "fallout4")
        {
            return CreateFallout4Plugin(
                path,
                name,
                Fallout4ModHeader.HeaderFlag.Localized
                | (lightByHeader ? Fallout4ModHeader.HeaderFlag.Small : 0)).FormKey;
        }

        var mod = new SkyrimMod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            SkyrimRelease.SkyrimSE);
        mod.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Localized;
        if (lightByHeader)
        {
            mod.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Small;
        }
        var weapon = mod.Weapons.AddNew();
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite
            .ToPath(path)
            .WithLoadOrderFromHeaderMasters()
            .WithNoDataFolder()
            .WithMastersListContent(MastersListContentOption.NoCheck)
            .Write();
        return weapon.FormKey;
    }

    private void CreateSkyrimLocalizedTableTypePlugin(string path)
    {
        var mod = new SkyrimMod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            SkyrimRelease.SkyrimSE);
        mod.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Localized;
        var spell = mod.Spells.AddNew();
        spell.EditorID = "FixtureSpell";
        spell.Name = "Localized spell";
        spell.Description = "Localized spell description";
        var topic = mod.DialogTopics.AddNew();
        topic.EditorID = "FixtureTopic";
        var responseRecord = new Mutagen.Bethesda.Skyrim.DialogResponses(
            mod.GetNextFormKey(),
            SkyrimRelease.SkyrimSE);
        topic.Responses.Add(responseRecord);
        responseRecord.Responses.Add(new Mutagen.Bethesda.Skyrim.DialogResponse
        {
            Text = "Localized response",
        });
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite
            .ToPath(path)
            .WithLoadOrderFromHeaderMasters()
            .WithNoDataFolder()
            .WithMastersListContent(MastersListContentOption.NoCheck)
            .Write();
    }

    private FormKey CreateGamePlugin(
        string game,
        string path,
        string name,
        bool lightByHeader = false)
    {
        if (game == "fallout4")
        {
            return CreateFallout4Plugin(
                path,
                name,
                lightByHeader ? Fallout4ModHeader.HeaderFlag.Small : 0).FormKey;
        }
        return CreateSkyrimPlugin(path, name, lightByHeader).FormKey;
    }

    private void CreateGamePluginWithMasters(
        string game,
        string path,
        string name,
        uint localId,
        params string[] masters)
    {
        if (game == "fallout4")
        {
            var mod = new Fallout4Mod(
                ModKey.FromNameAndExtension(Path.GetFileName(path)),
                Fallout4Release.Fallout4);
            foreach (var master in masters)
            {
                ((IMod)mod).MasterReferences.Add(new MasterReference
                {
                    Master = ModKey.FromNameAndExtension(master),
                });
            }
            var weapon = mod.Weapons.AddNew(new FormKey(mod.ModKey, localId));
            weapon.EditorID = "FixtureWeapon";
            weapon.Name = name;
            WriteFallout4(mod, path);
            return;
        }

        var skyrim = new SkyrimMod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            SkyrimRelease.SkyrimSE);
        foreach (var master in masters)
        {
            ((IMod)skyrim).MasterReferences.Add(new MasterReference
            {
                Master = ModKey.FromNameAndExtension(master),
            });
        }
        var skyrimWeapon = skyrim.Weapons.AddNew(new FormKey(skyrim.ModKey, localId));
        skyrimWeapon.EditorID = "FixtureWeapon";
        skyrimWeapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        skyrim.BeginWrite
            .ToPath(path)
            .WithLoadOrderFromHeaderMasters()
            .WithNoDataFolder()
            .WithMastersListContent(MastersListContentOption.NoCheck)
            .Write();
    }

    private void CreateLocalizedGamePluginWithMasters(
        string game,
        string path,
        string name,
        uint localId,
        params string[] masters)
    {
        if (game == "fallout4")
        {
            var mod = new Fallout4Mod(
                ModKey.FromNameAndExtension(Path.GetFileName(path)),
                Fallout4Release.Fallout4);
            mod.ModHeader.Flags |= Fallout4ModHeader.HeaderFlag.Localized;
            foreach (var master in masters)
            {
                ((IMod)mod).MasterReferences.Add(new MasterReference
                {
                    Master = ModKey.FromNameAndExtension(master),
                });
            }
            var weapon = mod.Weapons.AddNew(new FormKey(mod.ModKey, localId));
            weapon.EditorID = "FixtureWeapon";
            weapon.Name = name;
            WriteFallout4(mod, path);
            return;
        }

        var skyrim = new SkyrimMod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            SkyrimRelease.SkyrimSE);
        skyrim.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Localized;
        foreach (var master in masters)
        {
            ((IMod)skyrim).MasterReferences.Add(new MasterReference
            {
                Master = ModKey.FromNameAndExtension(master),
            });
        }
        var skyrimWeapon = skyrim.Weapons.AddNew(new FormKey(skyrim.ModKey, localId));
        skyrimWeapon.EditorID = "FixtureWeapon";
        skyrimWeapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        skyrim.BeginWrite
            .ToPath(path)
            .WithLoadOrderFromHeaderMasters()
            .WithNoDataFolder()
            .WithMastersListContent(MastersListContentOption.NoCheck)
            .Write();
    }

    private void CreateGamePluginOverride(
        string game,
        string path,
        string name,
        uint localId,
        string ownerMaster,
        params string[] masters)
    {
        var owner = ModKey.FromNameAndExtension(ownerMaster);
        if (game == "fallout4")
        {
            var mod = new Fallout4Mod(
                ModKey.FromNameAndExtension(Path.GetFileName(path)),
                Fallout4Release.Fallout4);
            foreach (var master in masters)
            {
                ((IMod)mod).MasterReferences.Add(new MasterReference
                {
                    Master = ModKey.FromNameAndExtension(master),
                });
            }
            var weapon = mod.Weapons.AddNew(new FormKey(owner, localId));
            weapon.EditorID = "FixtureWeapon";
            weapon.Name = name;
            WriteFallout4(mod, path);
            return;
        }

        var skyrim = new SkyrimMod(
            ModKey.FromNameAndExtension(Path.GetFileName(path)),
            SkyrimRelease.SkyrimSE);
        foreach (var master in masters)
        {
            ((IMod)skyrim).MasterReferences.Add(new MasterReference
            {
                Master = ModKey.FromNameAndExtension(master),
            });
        }
        var skyrimWeapon = skyrim.Weapons.AddNew(new FormKey(owner, localId));
        skyrimWeapon.EditorID = "FixtureWeapon";
        skyrimWeapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        skyrim.BeginWrite
            .ToPath(path)
            .WithLoadOrderFromHeaderMasters()
            .WithNoDataFolder()
            .WithMastersListContent(MastersListContentOption.NoCheck)
            .Write();
    }

    private void CreateSkyrimMessagePlugin(string path, params string[] buttons)
    {
        var mod = new SkyrimMod(ModKey.FromNameAndExtension(Path.GetFileName(path)), SkyrimRelease.SkyrimSE);
        var message = mod.Messages.AddNew();
        message.EditorID = "FixtureMessage";
        foreach (var text in buttons)
        {
            message.MenuButtons.Add(new Mutagen.Bethesda.Skyrim.MessageButton { Text = text });
        }
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite.ToPath(path).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
    }

    private void CreateSkyrimDialogPlugin(string path, params string[] responses)
    {
        var mod = new SkyrimMod(ModKey.FromNameAndExtension(Path.GetFileName(path)), SkyrimRelease.SkyrimSE);
        var topic = mod.DialogTopics.AddNew();
        topic.EditorID = "FixtureTopic";
        var responseRecord = new Mutagen.Bethesda.Skyrim.DialogResponses(
            mod.GetNextFormKey(),
            SkyrimRelease.SkyrimSE);
        topic.Responses.Add(responseRecord);
        foreach (var text in responses)
        {
            responseRecord.Responses.Add(new Mutagen.Bethesda.Skyrim.DialogResponse { Text = text });
        }
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite.ToPath(path).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
    }

    private static void WriteFallout4(Fallout4Mod mod, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite.ToPath(path).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
    }

    private ProcessResult RunAdapter(
        string game,
        string input,
        string rows,
        string output,
        string report,
        bool dryRun = false,
        string? masterStyleManifest = null) =>
        RunAdapter(
            "apply",
            game,
            input,
            rows,
            output,
            report,
            dryRun,
            masterStyleManifest: masterStyleManifest);

    private ProcessResult RunIdentityCheck(string command, string game, string mutagenRelease)
    {
        var dll = Path.Combine(AppContext.BaseDirectory, "SkyrimPluginTextTool.dll");
        var startInfo = new ProcessStartInfo(ResolveDotnetHost())
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = _root,
        };
        foreach (var arg in new[]
                 {
                     dll, command, "--game", game,
                     "--mutagen-release", mutagenRelease,
                     "--capability-level", "stable",
                 })
        {
            startInfo.ArgumentList.Add(arg);
        }
        using var process = Process.Start(startInfo)!;
        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        process.WaitForExit();
        return new ProcessResult(process.ExitCode, stdout, stderr);
    }

    private ProcessResult RunExportAdapter(
        string game,
        string input,
        string output,
        string report,
        string? capabilityLevel = null,
        string? masterStyleManifest = null,
        string command = "export",
        bool requireCompleteMasterStyleMap = false)
    {
        var dll = Path.Combine(AppContext.BaseDirectory, "SkyrimPluginTextTool.dll");
        var startInfo = new ProcessStartInfo(ResolveDotnetHost())
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = _root,
        };
        foreach (var arg in new[]
                 {
                     dll, command, "--game", game,
                     "--mutagen-release", MutagenRelease(game),
                     "--capability-level", capabilityLevel ?? CapabilityLevel(game),
                     "--project-root", _root,
                     "--input-plugin", input, "--output-jsonl", output, "--report", report,
                 })
        {
            startInfo.ArgumentList.Add(arg);
        }
        if (!string.IsNullOrWhiteSpace(masterStyleManifest))
        {
            startInfo.ArgumentList.Add("--master-style-manifest");
            startInfo.ArgumentList.Add(masterStyleManifest);
        }
        if (requireCompleteMasterStyleMap)
        {
            startInfo.ArgumentList.Add("--require-complete-master-style-map");
        }
        using var process = Process.Start(startInfo)!;
        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        process.WaitForExit();
        return new ProcessResult(process.ExitCode, stdout, stderr);
    }

    private ProcessResult RunLocalizedInventoryAdapter(
        string game,
        string input,
        string output,
        string report,
        string? masterStyleManifest = null,
        bool requireCompleteMasterStyleMap = false) =>
        RunExportAdapter(
            game,
            input,
            output,
            report,
            capabilityLevel: "read_only",
            masterStyleManifest: masterStyleManifest,
            command: "localized-inventory",
            requireCompleteMasterStyleMap: requireCompleteMasterStyleMap);

    private ProcessResult RunAdapter(
        string command,
        string game,
        string input,
        string rows,
        string output,
        string report,
        bool dryRun = false,
        string? mutagenRelease = null,
        string? capabilityLevel = null,
        string? masterStyleManifest = null)
    {
        var dll = Path.Combine(AppContext.BaseDirectory, "SkyrimPluginTextTool.dll");
        var startInfo = new ProcessStartInfo(ResolveDotnetHost())
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = _root,
        };
        foreach (var arg in new[]
                 {
                     dll, command, "--game", game,
                     "--mutagen-release", mutagenRelease ?? MutagenRelease(game),
                     "--capability-level", capabilityLevel ?? CapabilityLevel(game),
                     "--project-root", _root,
                     "--input-plugin", input, "--translation-jsonl", rows,
                     "--output-plugin", output, "--report", report,
                 })
        {
            startInfo.ArgumentList.Add(arg);
        }
        if (dryRun)
        {
            startInfo.ArgumentList.Add("--dry-run");
        }
        if (!string.IsNullOrWhiteSpace(masterStyleManifest))
        {
            startInfo.ArgumentList.Add("--master-style-manifest");
            startInfo.ArgumentList.Add(masterStyleManifest);
        }
        using var process = Process.Start(startInfo)!;
        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        process.WaitForExit();
        return new ProcessResult(process.ExitCode, stdout, stderr);
    }

    private FixturePaths ApplyFalloutFixtureWithUnknownSubrecord()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "Fixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "Fixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "Fixture.zh.jsonl");
        var applyReport = PathFor("qa", "Fixture.apply.md");
        var verifyReport = PathFor("qa", "Fixture.verify.md");
        var weapon = CreateFallout4Plugin(input, "Laser Rifle");
        WriteRows(rows, Row("fallout4", "Fixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Laser Rifle", "Translated Laser Rifle"));
        var applied = RunAdapter("apply", "fallout4", input, rows, output, applyReport);
        Assert.True(applied.ExitCode == 0, applied.Stdout + applied.Stderr + File.ReadAllText(applyReport));
        return new FixturePaths(input, output, rows, applyReport, verifyReport);
    }

    private static void MutateAsciiPayload(string path, string source, string target)
    {
        var bytes = File.ReadAllBytes(path);
        var sourceBytes = System.Text.Encoding.ASCII.GetBytes(source);
        var targetBytes = System.Text.Encoding.ASCII.GetBytes(target);
        var index = bytes.AsSpan().IndexOf(sourceBytes);
        Assert.True(index >= 0);
        targetBytes.CopyTo(bytes, index);
        File.WriteAllBytes(path, bytes);
    }

    private static void MutateRecordFormId(
        string path,
        string recordType,
        uint expectedFormId,
        uint formId)
    {
        var bytes = File.ReadAllBytes(path);
        var recordOffset = FindRecordOffset(bytes, recordType, expectedFormId);
        BitConverter.GetBytes(formId).CopyTo(bytes, recordOffset + 12);
        File.WriteAllBytes(path, bytes);
    }

    private static void MutateFirstRecordFormId(
        string path,
        string recordType,
        uint formId)
    {
        var bytes = File.ReadAllBytes(path);
        var marker = System.Text.Encoding.ASCII.GetBytes(recordType);
        var recordOffset = -1;
        for (var offset = 0; offset + 24 <= bytes.Length; offset++)
        {
            if (!bytes.AsSpan(offset, 4).SequenceEqual(marker)) continue;
            if (offset >= 8 && bytes.AsSpan(offset - 8, 4).SequenceEqual("GRUP"u8)) continue;
            var size = checked((int)BitConverter.ToUInt32(bytes, offset + 4));
            if (size >= 0 && offset + 24 + size <= bytes.Length)
            {
                recordOffset = offset;
                break;
            }
        }
        Assert.True(recordOffset >= 0, $"Major record was not found: {recordType}");
        BitConverter.GetBytes(formId).CopyTo(bytes, recordOffset + 12);
        File.WriteAllBytes(path, bytes);
    }

    private static void AssertRecordDataSize(
        string path,
        string recordType,
        uint expectedFormId,
        uint expectedDataSize)
    {
        var bytes = File.ReadAllBytes(path);
        var recordOffset = FindRecordOffset(bytes, recordType, expectedFormId);
        Assert.Equal(expectedDataSize, BitConverter.ToUInt32(bytes, recordOffset + 4));
    }

    private static void ClearRecordData(string path, string recordType, uint expectedFormId)
    {
        var bytes = File.ReadAllBytes(path);
        var recordOffset = FindRecordOffset(bytes, recordType, expectedFormId);
        var dataSize = checked((int)BitConverter.ToUInt32(bytes, recordOffset + 4));
        var groupOffset = recordOffset - 24;
        Assert.True(groupOffset >= 0);
        Assert.Equal("GRUP", System.Text.Encoding.ASCII.GetString(bytes, groupOffset, 4));
        var groupSize = checked((int)BitConverter.ToUInt32(bytes, groupOffset + 4));
        Assert.Equal(groupOffset + groupSize, recordOffset + 24 + dataSize);

        var compacted = new byte[bytes.Length - dataSize];
        bytes.AsSpan(0, recordOffset + 24).CopyTo(compacted);
        bytes.AsSpan(recordOffset + 24 + dataSize).CopyTo(compacted.AsSpan(recordOffset + 24));
        BitConverter.GetBytes(0u).CopyTo(compacted, recordOffset + 4);
        BitConverter.GetBytes(checked((uint)(groupSize - dataSize))).CopyTo(compacted, groupOffset + 4);
        File.WriteAllBytes(path, compacted);
    }

    private static int FindRecordOffset(byte[] bytes, string recordType, uint expectedFormId)
    {
        var signature = System.Text.Encoding.ASCII.GetBytes(recordType);
        var searchOffset = 0;
        var recordOffset = -1;
        while (searchOffset <= bytes.Length - signature.Length)
        {
            var relativeIndex = bytes.AsSpan(searchOffset).IndexOf(signature);
            if (relativeIndex < 0) break;
            var index = searchOffset + relativeIndex;
            if (index + 16 <= bytes.Length
                && BitConverter.ToUInt32(bytes, index + 12) == expectedFormId)
            {
                recordOffset = index;
                break;
            }
            searchOffset = index + signature.Length;
        }
        Assert.True(recordOffset >= 0, $"Record {recordType} {expectedFormId:X8} was not found.");
        return recordOffset;
    }

    private static void AssertReportTraits(
        string report,
        bool? localized,
        bool? lightByExtension,
        bool? lightByHeader,
        bool? containsUnsupportedLightFormIds)
    {
        var text = File.ReadAllText(report);
        Assert.Contains($"- localized: {TraitText(localized)}", text);
        Assert.Contains($"- light_by_extension: {TraitText(lightByExtension)}", text);
        Assert.Contains($"- light_by_header: {TraitText(lightByHeader)}", text);
        Assert.Contains(
            $"- contains_unsupported_light_formids: {TraitText(containsUnsupportedLightFormIds)}",
            text);
    }

    private static void AssertReportStatus(string report, string expectedStatus)
    {
        var statusLine = Assert.Single(
            File.ReadAllLines(report),
            static line => line.StartsWith("- Status:", StringComparison.Ordinal));
        Assert.Equal($"- Status: {expectedStatus}", statusLine);
    }

    private static string TraitText(bool? value) => value switch
    {
        true => "true",
        false => "false",
        null => "unknown",
    };

    private static Dictionary<string, JsonElement> ReadSingleRow(string output)
    {
        return Assert.Single(
            File.ReadAllLines(output)
                .Where(static line => !string.IsNullOrWhiteSpace(line))
                .Select(static line =>
                    JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(line)!));
    }

    private static void AssertCanonicalLightIdentity(
        IReadOnlyDictionary<string, JsonElement> row,
        string ownerModKey,
        uint localId)
    {
        Assert.True(
            row.ContainsKey("owner_mod_key"),
            $"Export row did not contain canonical light identity: {JsonSerializer.Serialize(row)}");
        Assert.Equal(ownerModKey, row["owner_mod_key"].GetString());
        Assert.Equal(localId, row["local_id"].GetUInt32());
        Assert.Equal("light", row["master_style"].GetString());
        Assert.False(string.IsNullOrWhiteSpace(row["master_style_evidence"].GetString()));
    }

    private void AssertLocalizedLightContext(string game, string input, string report)
    {
        var context = ExpectedMasterStyleContext(input);
        Assert.True(File.Exists(context));
        var payload = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(
            File.ReadAllText(context))!;
        Assert.Equal(game, payload["game_id"].GetString());
        Assert.Equal("light", payload["current_style"].GetString());
        Assert.Equal(Path.GetFileName(input), payload["plugin"].GetString());
        Assert.Equal(
            Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(input))),
            payload["input_sha256"].GetString());
        Assert.Contains(
            "workspace-header:",
            payload["current_evidence_source"].GetString(),
            StringComparison.Ordinal);
        Assert.Contains(
            Path.GetRelativePath(_root, context).Replace('\\', '/'),
            File.ReadAllText(report).Replace('\\', '/'),
            StringComparison.Ordinal);
    }

    private string ExpectedMasterStyleContext(string input) =>
        PluginMasterStyleContext.ContextPathFor(
            _root,
            input,
            ModKey.FromNameAndExtension(Path.GetFileName(input)));

    private static void AssertOrdinarySchemaV2Identity(
        IReadOnlyDictionary<string, JsonElement> row)
    {
        Assert.False(row.ContainsKey("owner_mod_key"));
        Assert.False(row.ContainsKey("local_id"));
        Assert.False(row.ContainsKey("master_style"));
        Assert.False(row.ContainsKey("master_style_evidence"));
    }

    private static (FormKey FormKey, string Name) ReadSingleWeapon(
        string game,
        string pluginPath)
    {
        if (game == "fallout4")
        {
            var weapon = Fallout4Mod.CreateFromBinary(pluginPath, Fallout4Release.Fallout4)
                .Weapons
                .Single();
            return (weapon.FormKey, weapon.Name?.String ?? string.Empty);
        }
        var skyrimWeapon = SkyrimMod.CreateFromBinary(pluginPath, SkyrimRelease.SkyrimSE)
            .Weapons
            .Single();
        return (skyrimWeapon.FormKey, skyrimWeapon.Name?.String ?? string.Empty);
    }

    private static bool IsSmallFlagged(string game, string pluginPath)
    {
        if (game == "fallout4")
        {
            return Fallout4Mod.CreateFromBinary(pluginPath, Fallout4Release.Fallout4)
                .ModHeader
                .Flags
                .HasFlag(Fallout4ModHeader.HeaderFlag.Small);
        }
        return SkyrimMod.CreateFromBinary(pluginPath, SkyrimRelease.SkyrimSE)
            .ModHeader
            .Flags
            .HasFlag(SkyrimModHeader.HeaderFlag.Small);
    }

    private static string ReportText(string report) =>
        File.Exists(report) ? Environment.NewLine + File.ReadAllText(report) : string.Empty;

    private void WriteMasterStyleManifest(
        string path,
        string game,
        string plugin,
        params (string ModKey, string Style, string InspectedPath)[] masters)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var payload = new
        {
            schema_version = 2,
            game_id = game,
            plugin,
            masters = masters.Select(master => new
            {
                mod_key = master.ModKey,
                master_style = master.Style,
                inspected_path = Path.GetRelativePath(_root, master.InspectedPath).Replace('\\', '/'),
                inspected_sha256 = Sha256(master.InspectedPath),
                small_flag = IsSmallFlagged(game, master.InspectedPath),
            }),
        };
        File.WriteAllText(path, JsonSerializer.Serialize(payload));
    }

    private static void MutateFirstAsciiByte(string path, string source, byte target)
    {
        var bytes = File.ReadAllBytes(path);
        var sourceBytes = System.Text.Encoding.ASCII.GetBytes(source);
        var index = bytes.AsSpan().IndexOf(sourceBytes);
        Assert.True(index >= 0);
        bytes[index] = target;
        File.WriteAllBytes(path, bytes);
    }

    private static void MutateRecordFlags(string path, string signature, uint flag)
    {
        var bytes = File.ReadAllBytes(path);
        var offset = FindRecordOffset(bytes, signature);
        var flags = BitConverter.ToUInt32(bytes, offset + 8);
        BitConverter.GetBytes(flags ^ flag).CopyTo(bytes, offset + 8);
        File.WriteAllBytes(path, bytes);
    }

    private static byte[] ReadTes4Hedr(string path)
    {
        var bytes = File.ReadAllBytes(path);
        Assert.True(bytes.Length >= 42);
        Assert.True(bytes.AsSpan(0, 4).SequenceEqual("TES4"u8));
        Assert.True(bytes.AsSpan(24, 4).SequenceEqual("HEDR"u8));
        Assert.Equal((ushort)12, BitConverter.ToUInt16(bytes, 28));
        return bytes.AsSpan(30, 12).ToArray();
    }

    private static void SetTes4Hedr(string path, float version, uint recordCount, uint nextObjectId)
    {
        var bytes = File.ReadAllBytes(path);
        _ = ReadTes4Hedr(path);
        BitConverter.GetBytes(version).CopyTo(bytes, 30);
        BitConverter.GetBytes(recordCount).CopyTo(bytes, 34);
        BitConverter.GetBytes(nextObjectId).CopyTo(bytes, 38);
        File.WriteAllBytes(path, bytes);
    }

    private static void SetFirstSubrecordUInt32(
        string path,
        string recordSignature,
        string subrecordSignature,
        uint value)
    {
        var bytes = File.ReadAllBytes(path);
        var recordOffset = FindRecordOffset(bytes, recordSignature);
        var recordSize = checked((int)BitConverter.ToUInt32(bytes, recordOffset + 4));
        var cursor = recordOffset + 24;
        var end = cursor + recordSize;
        var marker = System.Text.Encoding.ASCII.GetBytes(subrecordSignature);
        while (cursor + 6 <= end)
        {
            var length = BitConverter.ToUInt16(bytes, cursor + 4);
            if (cursor + 6 + length > end)
            {
                break;
            }
            if (bytes.AsSpan(cursor, 4).SequenceEqual(marker))
            {
                Assert.Equal(sizeof(uint), length);
                BitConverter.GetBytes(value).CopyTo(bytes, cursor + 6);
                File.WriteAllBytes(path, bytes);
                return;
            }
            cursor += 6 + length;
        }
        throw new InvalidDataException(
            $"Subrecord not found: {recordSignature}/{subrecordSignature}");
    }

    private static void AppendSubrecord(string path, string signature, string subrecordSignature, byte[] payload)
    {
        var bytes = File.ReadAllBytes(path);
        var recordOffset = FindRecordOffset(bytes, signature);
        var recordSize = checked((int)BitConverter.ToUInt32(bytes, recordOffset + 4));
        var insertion = recordOffset + 24 + recordSize;
        var extra = System.Text.Encoding.ASCII.GetBytes(subrecordSignature)
            .Concat(BitConverter.GetBytes(checked((ushort)payload.Length)))
            .Concat(payload)
            .ToArray();
        var updated = bytes[..insertion].Concat(extra).Concat(bytes[insertion..]).ToArray();
        BitConverter.GetBytes(checked((uint)(recordSize + extra.Length))).CopyTo(updated, recordOffset + 4);
        for (var offset = 0; offset + 24 <= bytes.Length; offset++)
        {
            if (!bytes.AsSpan(offset, 4).SequenceEqual("GRUP"u8)) continue;
            var size = checked((int)BitConverter.ToUInt32(bytes, offset + 4));
            if (offset < insertion && insertion <= offset + size)
            {
                BitConverter.GetBytes(checked((uint)(size + extra.Length))).CopyTo(updated, offset + 4);
            }
        }
        File.WriteAllBytes(path, updated);
    }

    private static int FindRecordOffset(byte[] bytes, string signature)
    {
        var marker = System.Text.Encoding.ASCII.GetBytes(signature);
        for (var offset = 0; offset + 24 <= bytes.Length; offset++)
        {
            if (!bytes.AsSpan(offset, 4).SequenceEqual(marker)) continue;
            if (offset >= 8 && bytes.AsSpan(offset - 8, 4).SequenceEqual("GRUP"u8)) continue;
            var size = checked((int)BitConverter.ToUInt32(bytes, offset + 4));
            if (size >= 0 && offset + 24 + size <= bytes.Length) return offset;
        }
        throw new InvalidDataException($"Record not found: {signature}");
    }

    private static string ResolveDotnetHost()
    {
        var configured = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        return !string.IsNullOrWhiteSpace(configured) && File.Exists(configured) ? configured : "dotnet";
    }

    private static string MutagenRelease(string game) =>
        game == "fallout4" ? "Fallout4" : "SkyrimSE";

    private static string CapabilityLevel(string game) =>
        game == "fallout4" ? "experimental_write" : "stable";

    private static string[] FormKeys(IModGetter mod) =>
        mod.EnumerateMajorRecords().Select(record => record.FormKey.ToString()).OrderBy(value => value).ToArray();

    private static string[] Masters(IModGetter mod) =>
        mod.MasterReferences.Select(reference => reference.Master.ToString()).ToArray();

    private static string Sha256(string path) =>
        Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path)));

    private static Dictionary<string, object> Row(
        string game,
        string plugin,
        string recordType,
        uint localId,
        string fieldPath,
        string subrecordType,
        string source,
        string target,
        byte rawMasterIndex = 0)
    {
        var rawFormId = ((uint)rawMasterIndex << 24) | localId;
        return new Dictionary<string, object>
        {
            ["schema_version"] = 2,
            ["game_id"] = game,
            ["plugin"] = plugin,
            ["record_type"] = recordType,
            ["form_id"] = rawFormId.ToString("X8"),
            ["editor_id"] = recordType == "WEAP" ? "FixtureWeapon" : "",
            ["field_path"] = fieldPath,
            ["subrecord_type"] = subrecordType,
            ["subrecord_index"] = 2,
            ["source"] = source,
            ["target"] = target,
            ["risk"] = "candidate",
            ["writeback"] = "supported",
        };
    }

    private static void WriteRows(string path, params Dictionary<string, object>[] rows)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllLines(path, rows.Select(row => JsonSerializer.Serialize(row)));
    }

    private string PathFor(params string[] parts)
    {
        var path = parts.Aggregate(_root, Path.Combine);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        return path;
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_root, recursive: true);
        }
        catch
        {
            // Test cleanup must not hide the writeback assertion.
        }
    }

    private sealed record ProcessResult(int ExitCode, string Stdout, string Stderr);
    private sealed record FixturePaths(string Input, string Output, string Rows, string ApplyReport, string VerifyReport);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateHardLink(
        string fileName,
        string existingFileName,
        IntPtr securityAttributes);
}
