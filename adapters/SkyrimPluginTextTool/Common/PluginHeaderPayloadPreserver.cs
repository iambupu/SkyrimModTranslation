using System.Buffers.Binary;
using System.Text;

internal static class PluginHeaderPayloadPreserver
{
    private const int MajorRecordHeaderSize = 24;
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
        var bytes = File.ReadAllBytes(pluginPath);
        if (bytes.Length < MajorRecordHeaderSize
            || !bytes.AsSpan(0, 4).SequenceEqual("TES4"u8))
        {
            throw new InvalidDataException("Plugin does not start with a complete TES4 record.");
        }

        var flags = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(8, 4));
        if ((flags & CompressedRecordFlag) != 0)
        {
            throw new InvalidDataException("Compressed TES4 records are not supported.");
        }

        var dataSize = checked((int)BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(4, 4)));
        var dataEnd = checked(MajorRecordHeaderSize + dataSize);
        if (dataEnd > bytes.Length)
        {
            throw new InvalidDataException("TES4 record extends beyond the plugin file.");
        }

        PayloadLocation? found = null;
        var offset = MajorRecordHeaderSize;
        uint? extendedSize = null;
        while (offset < dataEnd)
        {
            if (offset + 6 > dataEnd)
            {
                throw new InvalidDataException("TES4 contains a truncated subrecord header.");
            }

            var signature = Encoding.ASCII.GetString(bytes, offset, 4);
            var shortSize = BinaryPrimitives.ReadUInt16LittleEndian(bytes.AsSpan(offset + 4, 2));
            offset += 6;
            if (signature == "XXXX")
            {
                if (shortSize != 4 || extendedSize is not null || offset + 4 > dataEnd)
                {
                    throw new InvalidDataException("TES4 contains an invalid XXXX subrecord.");
                }
                extendedSize = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset, 4));
                offset += 4;
                continue;
            }

            var payloadSize = checked((int)(extendedSize ?? shortSize));
            extendedSize = null;
            if (offset + payloadSize > dataEnd)
            {
                throw new InvalidDataException($"TES4/{signature} payload extends beyond the record.");
            }
            if (signature == "HEDR")
            {
                if (found is not null)
                {
                    throw new InvalidDataException("TES4 contains multiple HEDR subrecords.");
                }
                if (payloadSize != 12)
                {
                    throw new InvalidDataException($"TES4/HEDR payload must be 12 bytes, found {payloadSize}.");
                }
                found = new PayloadLocation(offset, bytes.AsSpan(offset, payloadSize).ToArray());
            }
            offset += payloadSize;
        }

        if (extendedSize is not null)
        {
            throw new InvalidDataException("TES4 contains an orphan XXXX subrecord.");
        }
        return found ?? throw new InvalidDataException("TES4/HEDR subrecord was not found.");
    }

    private sealed record PayloadLocation(int PayloadOffset, byte[] Payload);
}
