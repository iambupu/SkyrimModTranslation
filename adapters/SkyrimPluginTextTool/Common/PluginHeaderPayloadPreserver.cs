using System.Buffers.Binary;

internal static class PluginHeaderPayloadPreserver
{
    private const int MajorRecordHeaderSize = 24;
    private const int MaxHeaderDataBytes = 16 * 1024 * 1024;
    private const uint CompressedRecordFlag = 0x00040000;

    public static void RestoreTes4Hedr(string inputPlugin, string outputPlugin)
    {
        if (string.Equals(
                Path.GetFullPath(inputPlugin),
                Path.GetFullPath(outputPlugin),
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("TES4/HEDR restoration requires separate input and output files.");
        }

        var input = FindTes4Hedr(inputPlugin);
        var output = FindTes4Hedr(outputPlugin);
        if (input.Payload.Length != output.Payload.Length)
        {
            throw new InvalidDataException(
                $"TES4/HEDR payload size changed: {input.Payload.Length} -> {output.Payload.Length}");
        }
        if (input.Payload.SequenceEqual(output.Payload)) return;

        using var stream = new FileStream(outputPlugin, FileMode.Open, FileAccess.Write, FileShare.None);
        stream.Position = output.PayloadOffset;
        stream.Write(input.Payload);
        stream.Flush(flushToDisk: true);
    }

    private static PayloadLocation FindTes4Hedr(string pluginPath)
    {
        using var stream = new FileStream(
            pluginPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read);
        if (stream.Length < MajorRecordHeaderSize)
        {
            throw new InvalidDataException("Plugin does not start with a complete TES4 record.");
        }

        Span<byte> header = stackalloc byte[MajorRecordHeaderSize];
        stream.ReadExactly(header);
        if (!header[..4].SequenceEqual("TES4"u8))
        {
            throw new InvalidDataException("Plugin does not start with a complete TES4 record.");
        }

        var flags = BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(8, 4));
        if ((flags & CompressedRecordFlag) != 0)
        {
            throw new InvalidDataException("Compressed TES4 records are not supported.");
        }

        var rawDataSize = BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(4, 4));
        if (rawDataSize > MaxHeaderDataBytes)
        {
            throw new InvalidDataException(
                $"TES4 header data exceeds the bounded limit of {MaxHeaderDataBytes} bytes.");
        }
        var dataSize = (int)rawDataSize;
        if (MajorRecordHeaderSize + (long)dataSize > stream.Length)
        {
            throw new InvalidDataException("TES4 record extends beyond the plugin file.");
        }
        var recordData = new byte[dataSize];
        stream.ReadExactly(recordData);

        PayloadLocation? found = null;
        foreach (var subrecord in Tes4SubrecordReader.Read(
                     recordData,
                     MajorRecordHeaderSize,
                     "TES4"))
        {
            if (subrecord.Signature == "HEDR")
            {
                if (found is not null)
                {
                    throw new InvalidDataException("TES4 contains multiple HEDR subrecords.");
                }
                if (subrecord.PayloadSize != 12)
                {
                    throw new InvalidDataException(
                        $"TES4/HEDR payload must be 12 bytes, found {subrecord.PayloadSize}.");
                }
                found = new PayloadLocation(
                    subrecord.PayloadOffset,
                    recordData.AsSpan(
                        checked(subrecord.PayloadOffset - MajorRecordHeaderSize),
                        checked((int)subrecord.PayloadSize)).ToArray());
            }
        }
        return found ?? throw new InvalidDataException("TES4/HEDR subrecord was not found.");
    }

    private sealed record PayloadLocation(int PayloadOffset, byte[] Payload);
}
