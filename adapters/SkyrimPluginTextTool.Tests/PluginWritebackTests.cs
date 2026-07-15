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
        Assert.Contains("Binary invariant verified: True", reportText);
        Assert.Contains("Allowed header changes: GRUP header bytes 4..7", reportText);
        Assert.Contains("Reparse target: final-output", reportText);
        Assert.Contains("Structural validation target: final-output", reportText);
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
        Assert.Contains("Binary invariant verified: True", reportText);
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
        Assert.Contains("Binary invariant verified: True", File.ReadAllText(report));
    }

    [Fact]
    public void VerifyRejectsTamperedNonTargetPayload()
    {
        var fixture = ApplyFalloutFixtureWithUnknownSubrecord();
        MutateAsciiPayload(fixture.Output, "FixtureWeapon", "XixtureWeapon");

        var result = RunAdapter("verify", "fallout4", fixture.Input, fixture.Rows, fixture.Output, fixture.VerifyReport);

        Assert.Equal(2, result.ExitCode);
        Assert.Contains("Binary invariant verified: False", File.ReadAllText(fixture.VerifyReport));
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
    public void SkyrimSchemaV1StillWritesSuccessfully()
    {
        var input = PathFor("work", "extracted_mods", "TestMod", "SkyrimFixture.esp");
        var output = PathFor("out", "TestMod", "tool_outputs", "SkyrimFixture.esp");
        var rows = PathFor("translated", "plugin_exports", "TestMod", "SkyrimFixture.zh.jsonl");
        var report = PathFor("qa", "SkyrimFixture.write.md");
        var weapon = CreateSkyrimPlugin(input, "Steel Sword");
        var row = Row("skyrim-se", "SkyrimFixture.esp", "WEAP", weapon.FormKey.ID, "Name", "FULL", "Steel Sword", "Translated Sword");
        row["schema_version"] = 1;
        WriteRows(rows, row);
        var inputHash = Sha256(input);

        var result = RunAdapter("skyrim-se", input, rows, output, report);

        Assert.Equal(0, result.ExitCode);
        var reparsed = SkyrimMod.CreateFromBinary(output, SkyrimRelease.SkyrimSE);
        Assert.Equal("Translated Sword", reparsed.Weapons.Single().Name?.String);
        Assert.Equal(inputHash, Sha256(input));
        var reportText = File.ReadAllText(report);
        Assert.Contains("Reparse succeeded: True", reportText);
        Assert.Contains("Reparse target: temporary-output", reportText);
        Assert.Contains("Structural validation target: temporary-output", reportText);

        var verifyReport = PathFor("qa", "SkyrimFixture.verify.md");
        var verify = RunAdapter("verify", "skyrim-se", input, rows, output, verifyReport);
        Assert.Equal(0, verify.ExitCode);
        var verifyReportText = File.ReadAllText(verifyReport);
        Assert.Contains("Reparse target: final-output", verifyReportText);
        Assert.Contains("Structural validation target: final-output", verifyReportText);
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

    private FalloutWeapon CreateFallout4Plugin(string path, string name)
    {
        var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(path)), Fallout4Release.Fallout4);
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

    private Mutagen.Bethesda.Skyrim.Weapon CreateSkyrimPlugin(string path, string name)
    {
        var mod = new SkyrimMod(ModKey.FromNameAndExtension(Path.GetFileName(path)), SkyrimRelease.SkyrimSE);
        var weapon = mod.Weapons.AddNew();
        weapon.EditorID = "FixtureWeapon";
        weapon.Name = name;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        mod.BeginWrite.ToPath(path).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
        return weapon;
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
