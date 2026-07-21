using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

public sealed class StringTableAdapterTests
{
    [Theory]
    [InlineData(StringTableType.Strings)]
    [InlineData(StringTableType.DlStrings)]
    [InlineData(StringTableType.IlStrings)]
    public void ExportApplyVerifyPreservesIdsAndUnauthorizedValues(StringTableType type)
    {
        using var fixture = new AdapterFixture(type);
        Assert.Equal(0, fixture.Export());

        var rows = fixture.ReadRows();
        Assert.Equal(2, rows.Count);
        Assert.All(rows, row =>
        {
            Assert.Equal(2, row["schema_version"]!.GetValue<int>());
            Assert.Equal("skyrim-se", row["game_id"]!.GetValue<string>());
            Assert.Equal("Example", row["plugin_basename"]!.GetValue<string>());
            Assert.Equal(StringTableCodec.TypeName(type), row["table_type"]!.GetValue<string>());
            Assert.Equal("english", row["source_language"]!.GetValue<string>());
            Assert.Equal(fixture.SourceRelativePath, row["source_table_path"]!.GetValue<string>());
            Assert.Equal(64, row["source_table_sha256"]!.GetValue<string>().Length);
        });
        rows[0]["Result"] = "第一条";
        fixture.WriteRows(rows);

        Assert.Equal(0, fixture.Apply());
        Assert.Equal(0, fixture.Verify());

        var output = StringTableCodec.Read(fixture.OutputPath, "utf-8");
        Assert.Equal([10U, 20U], output.Entries.Select(entry => entry.Id));
        Assert.Equal("第一条", output.Entries[0].Value);
        Assert.Equal("Café", output.Entries[1].Value);
        Assert.Contains("Authorized replacements: 1", File.ReadAllText(fixture.VerifyReport));
    }

    [Fact]
    public void ApplyRejectsSourceHashAndTextDriftWithoutOutput()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Result"] = "译文";
        fixture.WriteRows(rows);
        File.WriteAllBytes(
            fixture.InputPath,
            StringTableCodec.Write(
                StringTableType.Strings,
                    [(10, "Changed"), (20, "Café")],
                    StringTableCodec.StrictEncoding("windows-1252")));

        Assert.Equal(1, fixture.Apply());
        Assert.False(File.Exists(fixture.OutputPath));
    }

    [Fact]
    public void ApplyFailurePreservesExistingOutput()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Source"] = "Stale source";
        rows[0]["Result"] = "译文";
        fixture.WriteRows(rows);
        var previousOutput = StringTableCodec.Write(
            StringTableType.Strings,
            [(10, "Previous"), (20, "Output")],
            Encoding.UTF8);
        Directory.CreateDirectory(Path.GetDirectoryName(fixture.OutputPath)!);
        File.WriteAllBytes(fixture.OutputPath, previousOutput);

        Assert.Equal(1, fixture.Apply());
        Assert.Equal(previousOutput, File.ReadAllBytes(fixture.OutputPath));
    }

    [Fact]
    public void ApplyRejectsDuplicateAndMissingIdentities()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Result"] = "译文";
        rows.Add(rows[0].DeepClone().AsObject());
        fixture.WriteRows(rows);
        Assert.Equal(1, fixture.Apply());
        Assert.False(File.Exists(fixture.OutputPath));

        rows.RemoveAt(rows.Count - 1);
        rows[0]["string_id"] = 999U;
        fixture.WriteRows(rows);
        Assert.Equal(1, fixture.Apply());
        Assert.False(File.Exists(fixture.OutputPath));
    }

    [Fact]
    public void ApplyRejectsCrossGameRowsAndWrongTargetFilename()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Result"] = "译文";
        rows[0]["game_id"] = "fallout4";
        fixture.WriteRows(rows);

        Assert.Equal(1, fixture.Apply());
        Assert.False(File.Exists(fixture.OutputPath));

        rows[0]["game_id"] = "skyrim-se";
        fixture.WriteRows(rows);
        Assert.Equal(1, fixture.Apply(outputName: $"Example_wrong.{StringTableCodec.TypeName(fixture.Type)}"));
    }

    [Fact]
    public void VerifyRejectsUnauthorizedLogicalChange()
    {
        using var fixture = new AdapterFixture(StringTableType.DlStrings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Result"] = "第一条";
        fixture.WriteRows(rows);
        Assert.Equal(0, fixture.Apply());

        File.WriteAllBytes(
            fixture.OutputPath,
            StringTableCodec.Write(
                fixture.Type,
                [(10, "第一条"), (20, "Unauthorized")],
                Encoding.UTF8));

        Assert.Equal(1, fixture.Verify());
    }

    [Fact]
    public void ApplyRejectsOutputThatExceedsConfiguredFileLimit()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        rows[0]["Result"] = new string('译', 200);
        fixture.WriteRows(rows);

        Assert.Equal(1, fixture.Apply(maxFileBytes: new FileInfo(fixture.InputPath).Length));
        Assert.False(File.Exists(fixture.OutputPath));
    }

    [Fact]
    public void ApplyRejectsInvalidUtf8InsideATranslationTarget()
    {
        using var fixture = new AdapterFixture(StringTableType.Strings);
        Assert.Equal(0, fixture.Export());
        var rows = fixture.ReadRows();
        const string marker = "__INVALID_UTF8_TARGET__";
        rows[0]["Result"] = marker;
        var serialized = Encoding.UTF8.GetBytes(
            string.Join("\n", rows.Select(row => row.ToJsonString())) + "\n");
        var markerBytes = Encoding.ASCII.GetBytes(marker);
        var markerOffset = serialized.AsSpan().IndexOf(markerBytes);
        Assert.True(markerOffset >= 0);
        var invalid = serialized[..markerOffset]
            .Concat(new byte[] { 0xFF })
            .Concat(serialized[(markerOffset + markerBytes.Length)..])
            .ToArray();
        File.WriteAllBytes(fixture.TranslationPath, invalid);

        Assert.Equal(1, fixture.Apply());
        Assert.False(File.Exists(fixture.OutputPath));
    }

    private sealed class AdapterFixture : IDisposable
    {
        private readonly DirectoryInfo _root;

        internal AdapterFixture(StringTableType type)
        {
            Type = type;
            _root = Directory.CreateTempSubdirectory("bethesda-string-adapter-");
            var extension = StringTableCodec.TypeName(type);
            InputPath = Path.Combine(_root.FullName, "mod", "Strings", $"Example_english.{extension}");
            TranslationPath = Path.Combine(_root.FullName, "source", "string_tables", $"Example_english.{extension}.jsonl");
            OutputPath = Path.Combine(_root.FullName, "out", "Example", "tool_outputs", "Strings", $"Example_chinese.{extension}");
            ExportReport = Path.Combine(_root.FullName, "qa", "export.md");
            ApplyReport = Path.Combine(_root.FullName, "qa", "apply.md");
            VerifyReport = Path.Combine(_root.FullName, "qa", "verify.md");
            Directory.CreateDirectory(Path.GetDirectoryName(InputPath)!);
            File.WriteAllBytes(
                InputPath,
                StringTableCodec.Write(
                    type,
                    [(10, "First"), (20, "Café")],
                    StringTableCodec.StrictEncoding("windows-1252")));
        }

        internal StringTableType Type { get; }
        internal string InputPath { get; }
        internal string TranslationPath { get; }
        internal string OutputPath { get; }
        internal string ExportReport { get; }
        internal string ApplyReport { get; }
        internal string VerifyReport { get; }
        internal string SourceRelativePath => Path.GetRelativePath(_root.FullName, InputPath).Replace('\\', '/');

        internal int Export() => Program.Main(
        [
            "export",
            "--game", "skyrim-se",
            "--capability-level", "read_only",
            "--project-root", _root.FullName,
            "--input-table", InputPath,
            "--source-encoding", "windows-1252",
            "--source-language", "english",
            "--output-jsonl", TranslationPath,
            "--report", ExportReport,
        ]);

        internal int Apply(string? outputName = null, long? maxFileBytes = null)
        {
            var output = outputName is null
                ? OutputPath
                : Path.Combine(Path.GetDirectoryName(OutputPath)!, outputName);
            var arguments = new List<string>
            {
                "apply",
                "--game", "skyrim-se",
                "--capability-level", "stable",
                "--project-root", _root.FullName,
                "--input-table", InputPath,
                "--source-encoding", "windows-1252",
                "--target-encoding", "utf-8",
                "--source-language", "english",
                "--target-language", "chinese",
                "--translation-jsonl", TranslationPath,
                "--output-table", output,
                "--report", ApplyReport,
            };
            if (maxFileBytes is not null)
            {
                arguments.Add("--max-file-bytes");
                arguments.Add(maxFileBytes.Value.ToString());
            }
            return Program.Main(arguments.ToArray());
        }

        internal int Verify() => Program.Main(
        [
            "verify",
            "--game", "skyrim-se",
            "--capability-level", "stable",
            "--project-root", _root.FullName,
            "--input-table", InputPath,
            "--source-encoding", "windows-1252",
            "--target-encoding", "utf-8",
            "--source-language", "english",
            "--target-language", "chinese",
            "--translation-jsonl", TranslationPath,
            "--output-table", OutputPath,
            "--report", VerifyReport,
        ]);

        internal List<JsonObject> ReadRows() => File.ReadAllLines(TranslationPath)
            .Where(line => !string.IsNullOrWhiteSpace(line))
            .Select(line => JsonNode.Parse(line)!.AsObject())
            .ToList();

        internal void WriteRows(IEnumerable<JsonObject> rows)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(TranslationPath)!);
            File.WriteAllText(
                TranslationPath,
                string.Join("\n", rows.Select(row => row.ToJsonString())) + "\n",
                new UTF8Encoding(false));
        }

        public void Dispose()
        {
            _root.Delete(recursive: true);
        }
    }
}
