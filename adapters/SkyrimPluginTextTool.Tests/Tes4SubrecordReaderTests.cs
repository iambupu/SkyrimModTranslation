using System.Buffers.Binary;
using System.Text;
using Mutagen.Bethesda.Plugins;
using Xunit;

public sealed class Tes4SubrecordReaderTests : IDisposable
{
    private readonly string _root = Path.Combine(
        Path.GetTempPath(),
        "SkyrimPluginTextToolTes4Tests",
        Guid.NewGuid().ToString("N"));

    [Fact]
    public void ExtendedOnamBeforeMasterIsParsedFromLargeHeader()
    {
        var onam = new byte[70_000];
        var data = ExtendedSubrecord("ONAM", onam)
            .Concat(Subrecord("MAST", Encoding.UTF8.GetBytes("SomeMaster.esp\0")))
            .ToArray();
        var plugin = WriteTes4("Patch.esp", data);

        var rows = Tes4SubrecordReader.Read(data, 24, "TES4");
        var metadata = PluginHeaderMetadata.Read(plugin);

        Assert.Equal(2, rows.Count);
        Assert.True(rows[0].UsesExtendedSize);
        Assert.Equal((uint)onam.Length, rows[0].PayloadSize);
        Assert.Equal("MAST", rows[1].Signature);
        Assert.Equal(
            ModKey.FromNameAndExtension("SomeMaster.esp"),
            Assert.Single(metadata.Masters));
    }

    [Fact]
    public void InvalidXxxxShortSizeIsRejected()
    {
        var data = Encoding.ASCII.GetBytes("XXXX")
            .Concat(BitConverter.GetBytes((ushort)2))
            .Concat(new byte[2])
            .ToArray();

        var error = Assert.Throws<InvalidDataException>(() => Tes4SubrecordReader.Read(data));

        Assert.Contains("short size 2", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void OrphanXxxxIsRejected()
    {
        var data = Encoding.ASCII.GetBytes("XXXX")
            .Concat(BitConverter.GetBytes((ushort)4))
            .Concat(BitConverter.GetBytes((uint)32))
            .ToArray();

        var error = Assert.Throws<InvalidDataException>(() => Tes4SubrecordReader.Read(data));

        Assert.Contains("orphan XXXX", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ConsecutiveXxxxSubrecordsAreRejected()
    {
        var marker = Encoding.ASCII.GetBytes("XXXX")
            .Concat(BitConverter.GetBytes((ushort)4))
            .Concat(BitConverter.GetBytes((uint)32));
        var data = marker.Concat(marker).ToArray();

        var error = Assert.Throws<InvalidDataException>(() => Tes4SubrecordReader.Read(data));

        Assert.Contains("consecutive XXXX", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ExtendedPayloadBeyondRecordBoundaryIsRejected()
    {
        var data = Encoding.ASCII.GetBytes("XXXX")
            .Concat(BitConverter.GetBytes((ushort)4))
            .Concat(BitConverter.GetBytes((uint)32))
            .Concat(Encoding.ASCII.GetBytes("ONAM"))
            .Concat(BitConverter.GetBytes((ushort)0))
            .Concat(new byte[8])
            .ToArray();

        var error = Assert.Throws<InvalidDataException>(() => Tes4SubrecordReader.Read(data));

        Assert.Contains("payload exceeds", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void OversizedTes4HeaderLengthIsRejectedBeforeAllocation()
    {
        var plugin = WriteRawTes4Header("Oversized.esp", uint.MaxValue);

        var error = Assert.Throws<InvalidDataException>(() => PluginHeaderMetadata.Read(plugin));

        Assert.Contains("bounded limit", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void HedrPreserverRejectsOversizedHeaderBeforeAllocation()
    {
        var input = WriteRawTes4Header("Input.esp", uint.MaxValue);
        var output = WriteRawTes4Header("Output.esp", uint.MaxValue);

        var error = Assert.Throws<InvalidDataException>(
            () => PluginHeaderPayloadPreserver.RestoreTes4Hedr(input, output));

        Assert.Contains("bounded limit", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void DuplicateMastersAreRejectedBeforeMasterStyleResolution()
    {
        var master = Subrecord("MAST", Encoding.UTF8.GetBytes("SomeMaster.esp\0"));
        var plugin = WriteTes4("Patch.esp", master.Concat(master).ToArray());

        var error = Assert.Throws<InvalidDataException>(() => PluginHeaderMetadata.Read(plugin));

        Assert.Contains("duplicate MAST", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void InvalidUtf8MasterNameIsRejected()
    {
        var plugin = WriteTes4(
            "Patch.esp",
            Subrecord("MAST", [0xFF, 0x00]));

        var error = Assert.Throws<InvalidDataException>(() => PluginHeaderMetadata.Read(plugin));

        Assert.Contains("invalid UTF-8", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void MasterNameContainingAPathIsRejected()
    {
        var plugin = WriteTes4(
            "Patch.esp",
            Subrecord("MAST", Encoding.UTF8.GetBytes("../SomeMaster.esm\0")));

        var error = Assert.Throws<InvalidDataException>(() => PluginHeaderMetadata.Read(plugin));

        Assert.Contains("invalid master name", error.Message, StringComparison.Ordinal);
    }

    public void Dispose()
    {
        if (Directory.Exists(_root)) Directory.Delete(_root, recursive: true);
    }

    private string WriteTes4(string name, byte[] data)
    {
        Directory.CreateDirectory(_root);
        var path = Path.Combine(_root, name);
        var header = new byte[24];
        Encoding.ASCII.GetBytes("TES4").CopyTo(header, 0);
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(4, 4), checked((uint)data.Length));
        File.WriteAllBytes(path, header.Concat(data).ToArray());
        return path;
    }

    private string WriteRawTes4Header(string name, uint dataSize)
    {
        Directory.CreateDirectory(_root);
        var path = Path.Combine(_root, name);
        var header = new byte[24];
        Encoding.ASCII.GetBytes("TES4").CopyTo(header, 0);
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(4, 4), dataSize);
        File.WriteAllBytes(path, header);
        return path;
    }

    private static byte[] Subrecord(string signature, byte[] payload) =>
        Encoding.ASCII.GetBytes(signature)
            .Concat(BitConverter.GetBytes(checked((ushort)payload.Length)))
            .Concat(payload)
            .ToArray();

    private static byte[] ExtendedSubrecord(string signature, byte[] payload) =>
        Encoding.ASCII.GetBytes("XXXX")
            .Concat(BitConverter.GetBytes((ushort)4))
            .Concat(BitConverter.GetBytes(checked((uint)payload.Length)))
            .Concat(Encoding.ASCII.GetBytes(signature))
            .Concat(BitConverter.GetBytes((ushort)0))
            .Concat(payload)
            .ToArray();
}
