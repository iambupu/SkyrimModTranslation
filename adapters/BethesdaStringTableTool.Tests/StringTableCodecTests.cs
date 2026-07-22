using System.Buffers.Binary;
using System.Text;
using Xunit;

public sealed class StringTableCodecTests
{
    [Theory]
    [InlineData(StringTableType.Strings)]
    [InlineData(StringTableType.DlStrings)]
    [InlineData(StringTableType.IlStrings)]
    public void RoundTripValidTables(StringTableType type)
    {
        var encoding = StringTableCodec.StrictEncoding("utf-8");
        var bytes = StringTableCodec.Write(
            type,
            [(20, "Second"), (10, "First")],
            encoding);

        var parsed = StringTableCodec.Parse(bytes, type, encoding);

        Assert.Equal(type, parsed.Type);
        Assert.Equal([(20U, "Second"), (10U, "First")], parsed.Entries.Select(entry => (entry.Id, entry.Value)));
        Assert.Equal(bytes.Length, parsed.FileSize);
    }

    [Fact]
    public void RejectsTruncatedHeader()
    {
        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse([0, 1, 2], StringTableType.Strings, Encoding.UTF8));
        Assert.Contains("header", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void RejectsDuplicateIds()
    {
        var bytes = StringTableCodec.Write(
            StringTableType.Strings,
            [(1, "First"), (2, "Second")],
            Encoding.UTF8);
        BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(16, 4), 1);

        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, StringTableType.Strings, Encoding.UTF8));
        Assert.Contains("repeats string ID", error.Message);
    }

    [Theory]
    [InlineData(StringTableType.Strings)]
    [InlineData(StringTableType.DlStrings)]
    [InlineData(StringTableType.IlStrings)]
    public void RejectsOffsetOutsideDataSection(StringTableType type)
    {
        var bytes = StringTableCodec.Write(type, [(1, "Value")], Encoding.UTF8);
        var dataSize = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(4, 4));
        BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(12, 4), dataSize);

        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, type, Encoding.UTF8));
        Assert.Contains("outside", error.Message);
    }

    [Fact]
    public void RejectsMissingStringsTerminator()
    {
        var bytes = StringTableCodec.Write(
            StringTableType.Strings,
            [(1, "Value")],
            Encoding.UTF8);
        bytes[^1] = (byte)'x';

        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, StringTableType.Strings, Encoding.UTF8));
        Assert.Contains("terminator", error.Message);
    }

    [Theory]
    [InlineData(StringTableType.DlStrings)]
    [InlineData(StringTableType.IlStrings)]
    public void RejectsLengthPastDataBoundary(StringTableType type)
    {
        var bytes = StringTableCodec.Write(type, [(1, "Value")], Encoding.UTF8);
        BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(16, 4), uint.MaxValue);

        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, type, Encoding.UTF8));
        Assert.Contains("boundary", error.Message);
    }

    [Fact]
    public void RejectsDeclaredDataSizeMismatch()
    {
        var bytes = StringTableCodec.Write(
            StringTableType.Strings,
            [(1, "Value")],
            Encoding.UTF8);
        BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(4, 4), 1);

        var error = Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, StringTableType.Strings, Encoding.UTF8));
        Assert.Contains("file boundary", error.Message);
    }

    [Fact]
    public void EnforcesEntryAndFileLimits()
    {
        var bytes = StringTableCodec.Write(
            StringTableType.Strings,
            [(1, "Value")],
            Encoding.UTF8);

        Assert.Throws<InvalidDataException>(() =>
            StringTableCodec.Parse(bytes, StringTableType.Strings, Encoding.UTF8, maxEntries: 1, maxFileBytes: 8));
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            StringTableCodec.Parse(bytes, StringTableType.Strings, Encoding.UTF8, maxEntries: 0));
    }

    [Fact]
    public void ReadRejectsOversizedFileBeforeParsing()
    {
        var root = Directory.CreateTempSubdirectory("bethesda-string-table-limit-");
        try
        {
            var input = Path.Combine(root.FullName, "Example_english.strings");
            File.WriteAllBytes(input, new byte[16]);

            var error = Assert.Throws<InvalidDataException>(() =>
                StringTableCodec.Read(input, "utf-8", maxFileBytes: 8));

            Assert.Contains("file-size limit", error.Message);
        }
        finally
        {
            root.Delete(recursive: true);
        }
    }

    [Fact]
    public void LargeTableRoundTripsWithinDeclaredBounds()
    {
        const int count = 50_000;
        var entries = Enumerable.Range(1, count)
            .Select(index => ((uint)index, $"Value {index}"))
            .ToArray();

        var bytes = StringTableCodec.Write(
            StringTableType.Strings,
            entries,
            Encoding.UTF8);
        var parsed = StringTableCodec.Parse(
            bytes,
            StringTableType.Strings,
            Encoding.UTF8,
            maxEntries: count,
            maxFileBytes: bytes.Length);

        Assert.Equal(count, parsed.Entries.Count);
        Assert.Equal((1U, "Value 1"), (parsed.Entries[0].Id, parsed.Entries[0].Value));
        Assert.Equal(
            ((uint)count, $"Value {count}"),
            (parsed.Entries[^1].Id, parsed.Entries[^1].Value));
    }

    [Fact]
    public void InventoryReportsBoundMetadataWithoutChangingInput()
    {
        var root = Directory.CreateTempSubdirectory("bethesda-string-table-");
        try
        {
            var input = Path.Combine(root.FullName, "Example_english.strings");
            var report = Path.Combine(root.FullName, "qa", "inventory.md");
            var bytes = StringTableCodec.Write(
                StringTableType.Strings,
                [(10, "First"), (20, "Second")],
                Encoding.UTF8);
            File.WriteAllBytes(input, bytes);

            var exitCode = Program.Main(
            [
                "inventory",
                "--game", "skyrim-se",
                "--capability-level", "inventory_only",
                "--project-root", root.FullName,
                "--input-table", input,
                "--source-encoding", "utf-8",
                "--source-language", "english",
                "--report", report,
            ]);

            Assert.Equal(0, exitCode);
            Assert.Equal(bytes, File.ReadAllBytes(input));
            var markdown = File.ReadAllText(report);
            Assert.Contains("Plugin basename: Example", markdown);
            Assert.Contains("Language: english", markdown);
            Assert.Contains("Table type: strings", markdown);
            Assert.Contains("Entry count: 2", markdown);
            Assert.Contains("SHA256:", markdown);
        }
        finally
        {
            root.Delete(recursive: true);
        }
    }
}
