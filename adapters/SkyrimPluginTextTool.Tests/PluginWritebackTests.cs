using System.Diagnostics;
using System.Security.Cryptography;
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
        var reportText = File.ReadAllText(report);
        Assert.Contains("Operation: export", reportText);
        Assert.Contains("plugin_adapter: mutagen-bethesda-plugin", reportText);
        Assert.Contains("support_level: read_only", reportText);
        Assert.Contains("plugin_text_capability_level: read_only", reportText);
        Assert.Matches(@"Input SHA256: [0-9A-F]{64}", reportText);
        Assert.Matches(@"Output JSONL SHA256: [0-9A-F]{64}", reportText);
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
    public void Fallout4ExportRejectsUnsupportedLightFormIdWithoutOutput()
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
        Assert.Contains("0xFE/light FormID is unsupported", File.ReadAllText(report));
        AssertReportStatus(report, "blocked");
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: true);
    }

    [Fact]
    public void Fallout4ExportRejectsUnsupportedRecordWithLightFormIdBeforeWritingRows()
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
        Assert.Contains("0xFE/light FormID is unsupported", File.ReadAllText(report));
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: true);
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
        Assert.Contains("0xFE/light FormID is unsupported", File.ReadAllText(exportReport));
        AssertReportTraits(
            exportReport,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: true);

        var applied = RunAdapter("fallout4", input, applyRows, applyOutput, applyReport);

        Assert.Equal(2, applied.ExitCode);
        Assert.False(File.Exists(applyOutput));
        Assert.Contains("0xFE/light FormIDs", File.ReadAllText(applyReport));
        AssertReportTraits(
            applyReport,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: true);
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
        Assert.Contains("localized plugin requires", reportText, StringComparison.OrdinalIgnoreCase);
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
    public void Fallout4VerifyReportsZeroSubrecordLightFormId()
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

        Assert.Equal(0, result.ExitCode);
        AssertReportTraits(
            report,
            localized: false,
            lightByExtension: false,
            lightByHeader: false,
            containsUnsupportedLightFormIds: true);
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
    public void SkyrimEslWritebackIsBlocked()
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
        Assert.Contains("light plugin writeback is read-only", File.ReadAllText(report));
    }

    [Fact]
    public void SkyrimEslFlaggedEspWritebackIsBlocked()
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
        Assert.Contains("light plugin writeback is read-only", File.ReadAllText(report));
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
    public void LightFormIdIsExplicitlyUnsupported()
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
        Assert.Contains("0xFE/light FormID is unsupported", File.ReadAllText(report));
        AssertReportStatus(report, "blocked");
    }

    [Fact]
    public void Fallout4EslWritebackIsExplicitlyUnsupported()
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
        Assert.Contains("Fallout 4 ESL writeback is not supported", File.ReadAllText(report));
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

        Assert.Equal(2, result.ExitCode);
        Assert.False(File.Exists(output));
        Assert.Contains("Fallout 4 ESL writeback is not supported", File.ReadAllText(report));
        AssertReportTraits(
            report,
            localized: null,
            lightByExtension: true,
            lightByHeader: null,
            containsUnsupportedLightFormIds: null);
    }

    [Fact]
    public void Fallout4SmallFlaggedEspWritebackIsExplicitlyUnsupported()
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
        Assert.Contains("light plugin writeback is not supported", File.ReadAllText(report), StringComparison.OrdinalIgnoreCase);
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

    private ProcessResult RunAdapter(string game, string input, string rows, string output, string report, bool dryRun = false) =>
        RunAdapter("apply", game, input, rows, output, report, dryRun);

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
        string? capabilityLevel = null)
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
                     dll, "export", "--game", game,
                     "--mutagen-release", MutagenRelease(game),
                     "--capability-level", capabilityLevel ?? CapabilityLevel(game),
                     "--project-root", _root,
                     "--input-plugin", input, "--output-jsonl", output, "--report", report,
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

    private ProcessResult RunAdapter(
        string command,
        string game,
        string input,
        string rows,
        string output,
        string report,
        bool dryRun = false,
        string? mutagenRelease = null,
        string? capabilityLevel = null)
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
}
