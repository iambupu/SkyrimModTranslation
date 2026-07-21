using System.Buffers.Binary;
using System.Text;

internal sealed record Tes4Subrecord(
    string Signature,
    int HeaderOffset,
    int PayloadOffset,
    uint PayloadSize,
    bool UsesExtendedSize);

internal static class Tes4SubrecordReader
{
    public static IReadOnlyList<Tes4Subrecord> Read(
        ReadOnlySpan<byte> data,
        int baseOffset = 0,
        string context = "TES4")
    {
        var rows = new List<Tes4Subrecord>();
        var offset = 0;
        uint? extendedSize = null;
        while (offset < data.Length)
        {
            if (offset + 6 > data.Length)
            {
                throw new InvalidDataException($"{context} contains a truncated subrecord header");
            }

            var headerOffset = offset;
            var signature = Encoding.ASCII.GetString(data.Slice(offset, 4));
            var shortSize = BinaryPrimitives.ReadUInt16LittleEndian(data.Slice(offset + 4, 2));
            offset += 6;
            if (signature == "XXXX")
            {
                if (shortSize != sizeof(uint))
                {
                    throw new InvalidDataException(
                        $"{context} contains an XXXX subrecord with short size {shortSize}, expected 4");
                }
                if (extendedSize is not null)
                {
                    throw new InvalidDataException($"{context} contains consecutive XXXX subrecords");
                }
                if (offset + sizeof(uint) > data.Length)
                {
                    throw new InvalidDataException($"{context} contains a truncated XXXX payload");
                }
                extendedSize = BinaryPrimitives.ReadUInt32LittleEndian(data.Slice(offset, sizeof(uint)));
                offset += sizeof(uint);
                continue;
            }

            var payloadSize = extendedSize ?? shortSize;
            var usesExtendedSize = extendedSize is not null;
            extendedSize = null;
            if (payloadSize > int.MaxValue || offset + (long)payloadSize > data.Length)
            {
                throw new InvalidDataException(
                    $"{context}/{signature} payload exceeds the record boundary");
            }
            rows.Add(new(
                signature,
                checked(baseOffset + headerOffset),
                checked(baseOffset + offset),
                payloadSize,
                usesExtendedSize));
            offset += checked((int)payloadSize);
        }

        if (extendedSize is not null)
        {
            throw new InvalidDataException($"{context} contains an orphan XXXX subrecord");
        }
        return rows;
    }
}
