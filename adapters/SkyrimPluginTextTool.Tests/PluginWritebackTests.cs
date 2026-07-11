using System.Diagnostics;
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

    private ProcessResult RunAdapter(string game, string input, string rows, string output, string report, bool dryRun = false)
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
                     dll, "apply", "--game", game, "--project-root", _root,
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

    private static string ResolveDotnetHost()
    {
        var configured = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        return !string.IsNullOrWhiteSpace(configured) && File.Exists(configured) ? configured : "dotnet";
    }

    private static string[] FormKeys(IModGetter mod) =>
        mod.EnumerateMajorRecords().Select(record => record.FormKey.ToString()).OrderBy(value => value).ToArray();

    private static string[] Masters(IModGetter mod) =>
        mod.MasterReferences.Select(reference => reference.Master.ToString()).ToArray();

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
            ["subrecord_index"] = 1,
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
}
