using System.Buffers.Binary;
using System.Text;

public enum StringTableType
{
    Strings,
    DlStrings,
    IlStrings,
}

public sealed record StringTableEntry(uint Id, uint Offset, string Value);

public sealed record StringTableDocument(
    StringTableType Type,
    IReadOnlyList<StringTableEntry> Entries,
    uint DataSize,
    long FileSize);

public static class StringTableCodec
{
    public const int DefaultMaxEntries = 2_000_000;
    public const long DefaultMaxFileBytes = 512L * 1024 * 1024;

    public static StringTableDocument Read(
        string path,
        string encodingName,
        int maxEntries = DefaultMaxEntries,
        long maxFileBytes = DefaultMaxFileBytes)
    {
        if (maxEntries <= 0 || maxFileBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxEntries), "String-table limits must be positive.");
        }
        var fileLength = new FileInfo(path).Length;
        if (fileLength > maxFileBytes)
        {
            throw new InvalidDataException("String table exceeds the configured file-size limit.");
        }
        var type = TypeFromPath(path);
        var bytes = File.ReadAllBytes(path);
        return Parse(bytes, type, StrictEncoding(encodingName), maxEntries, maxFileBytes);
    }

    public static StringTableDocument Parse(
        ReadOnlySpan<byte> bytes,
        StringTableType type,
        Encoding encoding,
        int maxEntries = DefaultMaxEntries,
        long maxFileBytes = DefaultMaxFileBytes)
    {
        if (maxEntries <= 0 || maxFileBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxEntries), "String-table limits must be positive.");
        }
        if (bytes.Length > maxFileBytes)
        {
            throw new InvalidDataException("String table exceeds the configured file-size limit.");
        }
        if (bytes.Length < 8)
        {
            throw new InvalidDataException("String table header is truncated.");
        }

        var count = BinaryPrimitives.ReadUInt32LittleEndian(bytes[..4]);
        var dataSize = BinaryPrimitives.ReadUInt32LittleEndian(bytes.Slice(4, 4));
        if (count > maxEntries)
        {
            throw new InvalidDataException("String table exceeds the configured entry-count limit.");
        }
        var directoryBytes = checked((long)count * 8L);
        var dataStart = checked(8L + directoryBytes);
        var expectedSize = checked(dataStart + dataSize);
        if (dataStart > bytes.Length)
        {
            throw new InvalidDataException("String table directory is truncated.");
        }
        if (expectedSize != bytes.Length)
        {
            throw new InvalidDataException("String table data size does not match the file boundary.");
        }

        var entries = new List<StringTableEntry>(checked((int)count));
        var ids = new HashSet<uint>();
        var data = bytes[(int)dataStart..];
        for (var index = 0; index < count; index++)
        {
            var directoryOffset = checked(8 + (int)index * 8);
            var id = BinaryPrimitives.ReadUInt32LittleEndian(bytes.Slice(directoryOffset, 4));
            var offset = BinaryPrimitives.ReadUInt32LittleEndian(bytes.Slice(directoryOffset + 4, 4));
            if (!ids.Add(id))
            {
                throw new InvalidDataException($"String table repeats string ID {id}.");
            }
            if (offset >= dataSize)
            {
                throw new InvalidDataException($"String ID {id} points outside the data section.");
            }
            var value = type == StringTableType.Strings
                ? ReadNullTerminated(data, offset, id, encoding)
                : ReadLengthPrefixed(data, offset, id, encoding);
            entries.Add(new StringTableEntry(id, offset, value));
        }

        return new StringTableDocument(type, entries, dataSize, bytes.Length);
    }

    public static byte[] Write(
        StringTableType type,
        IReadOnlyList<(uint Id, string Value)> entries,
        Encoding encoding)
    {
        ArgumentNullException.ThrowIfNull(entries);
        var seen = new HashSet<uint>();
        using var data = new MemoryStream();
        using var directory = new MemoryStream();
        var lengthBuffer = new byte[4];
        var directoryEntryBuffer = new byte[8];
        foreach (var (id, value) in entries)
        {
            if (!seen.Add(id))
            {
                throw new InvalidDataException($"String table repeats string ID {id}.");
            }
            if (value.Contains('\0'))
            {
                throw new InvalidDataException($"String ID {id} contains a NUL character.");
            }
            var offset = checked((uint)data.Length);
            var encoded = encoding.GetBytes(value);
            if (type != StringTableType.Strings)
            {
                BinaryPrimitives.WriteUInt32LittleEndian(
                    lengthBuffer,
                    checked((uint)encoded.Length + 1));
                data.Write(lengthBuffer);
            }
            data.Write(encoded);
            data.WriteByte(0);

            BinaryPrimitives.WriteUInt32LittleEndian(directoryEntryBuffer.AsSpan(0, 4), id);
            BinaryPrimitives.WriteUInt32LittleEndian(directoryEntryBuffer.AsSpan(4, 4), offset);
            directory.Write(directoryEntryBuffer);
        }

        using var output = new MemoryStream();
        Span<byte> header = stackalloc byte[8];
        BinaryPrimitives.WriteUInt32LittleEndian(header[..4], checked((uint)entries.Count));
        BinaryPrimitives.WriteUInt32LittleEndian(header[4..], checked((uint)data.Length));
        output.Write(header);
        directory.Position = 0;
        directory.CopyTo(output);
        data.Position = 0;
        data.CopyTo(output);
        return output.ToArray();
    }

    public static StringTableType TypeFromPath(string path) =>
        Path.GetExtension(path).ToLowerInvariant() switch
        {
            ".strings" => StringTableType.Strings,
            ".dlstrings" => StringTableType.DlStrings,
            ".ilstrings" => StringTableType.IlStrings,
            _ => throw new InvalidDataException("String table extension must be .strings, .dlstrings, or .ilstrings."),
        };

    public static string TypeName(StringTableType type) => type switch
    {
        StringTableType.Strings => "strings",
        StringTableType.DlStrings => "dlstrings",
        StringTableType.IlStrings => "ilstrings",
        _ => throw new ArgumentOutOfRangeException(nameof(type)),
    };

    public static Encoding StrictEncoding(string name)
    {
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        if (string.IsNullOrWhiteSpace(name))
        {
            throw new ArgumentException("String-table encoding must be non-empty.", nameof(name));
        }
        return Encoding.GetEncoding(
            name.Trim(),
            EncoderFallback.ExceptionFallback,
            DecoderFallback.ExceptionFallback);
    }

    private static string ReadNullTerminated(
        ReadOnlySpan<byte> data,
        uint offset,
        uint id,
        Encoding encoding)
    {
        var remaining = data[(int)offset..];
        var terminator = remaining.IndexOf((byte)0);
        if (terminator < 0)
        {
            throw new InvalidDataException($"String ID {id} has no NUL terminator.");
        }
        return Decode(remaining[..terminator], id, encoding);
    }

    private static string ReadLengthPrefixed(
        ReadOnlySpan<byte> data,
        uint offset,
        uint id,
        Encoding encoding)
    {
        if ((long)offset + 4 > data.Length)
        {
            throw new InvalidDataException($"String ID {id} has a truncated length prefix.");
        }
        var length = BinaryPrimitives.ReadUInt32LittleEndian(data.Slice((int)offset, 4));
        if (length == 0)
        {
            throw new InvalidDataException($"String ID {id} has an invalid zero length.");
        }
        var valueStart = checked((long)offset + 4L);
        var valueEnd = checked(valueStart + length);
        if (valueEnd > data.Length)
        {
            throw new InvalidDataException($"String ID {id} exceeds the data section boundary.");
        }
        var valueBytes = data.Slice((int)valueStart, checked((int)length));
        if (valueBytes[^1] != 0)
        {
            throw new InvalidDataException($"String ID {id} length-prefixed value has no NUL terminator.");
        }
        return Decode(valueBytes[..^1], id, encoding);
    }

    private static string Decode(ReadOnlySpan<byte> bytes, uint id, Encoding encoding)
    {
        try
        {
            return encoding.GetString(bytes);
        }
        catch (DecoderFallbackException ex)
        {
            throw new InvalidDataException($"String ID {id} is invalid for encoding {encoding.WebName}.", ex);
        }
    }
}
