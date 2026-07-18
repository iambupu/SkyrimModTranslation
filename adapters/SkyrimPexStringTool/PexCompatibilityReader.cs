using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Pex;

internal sealed record PexReadResult(
    PexFile File,
    PexCompatibilityMetadata Compatibility);

internal sealed record PexCompatibilityMetadata(
    string Layout,
    int StringCount,
    string PostStringTablePayloadSha256,
    IReadOnlyList<PexDebugPropertyGroupMetadata> DebugPropertyGroups)
{
    public bool NormalizedOfficialFallout4Layout =>
        string.Equals(Layout, PexCompatibilityReader.OfficialFallout4Layout, StringComparison.Ordinal);
}

internal sealed record PexDebugPropertyGroupMetadata(
    string ObjectName,
    string GroupName,
    string DocString,
    uint UserFlags,
    IReadOnlyList<string> PropertyNames);

internal static class PexCompatibilityReader
{
    internal const string NativeLayout = "mutagen-native";
    internal const string OfficialFallout4Layout = "fallout4-official-v3.9";

    private const uint PexMagic = 0xFA57C0DE;

    public static PexReadResult ReadFromFile(string path, GameCategory category)
    {
        var bytes = File.ReadAllBytes(path);
        var envelope = ReadEnvelope(bytes, category);

        try
        {
            var file = ReadWithMutagen(bytes, category);
            return BuildResult(file, envelope, NativeLayout, []);
        }
        catch (Exception directError) when (IsParseFailure(directError) && category == GameCategory.Fallout4)
        {
            try
            {
                var normalized = NormalizeOfficialFallout4Layout(bytes, envelope);
                var file = ReadWithMutagen(normalized.Bytes, category);
                ValidateNormalizedGroups(file, normalized.PropertyGroups);
                return BuildResult(
                    file,
                    envelope,
                    OfficialFallout4Layout,
                    normalized.PropertyGroups);
            }
            catch (Exception compatibilityError) when (IsParseFailure(compatibilityError))
            {
                throw new InvalidDataException(
                    "Fallout 4 PEX parsing failed for both the Mutagen layout and the official "
                    + "Fallout 4 layout. "
                    + $"Mutagen: {directError.Message} Compatibility: {compatibilityError.Message}",
                    compatibilityError);
            }
        }
    }

    private static PexReadResult BuildResult(
        PexFile file,
        RawEnvelope envelope,
        string layout,
        IReadOnlyList<PexDebugPropertyGroupMetadata> groups) =>
        new(
            file,
            new PexCompatibilityMetadata(
                layout,
                envelope.Strings.Count,
                Convert.ToHexString(SHA256.HashData(envelope.PostStringTablePayload)),
                groups));

    private static PexFile ReadWithMutagen(byte[] bytes, GameCategory category)
    {
        using var stream = new MemoryStream(bytes, writable: false);
        return PexFile.CreateFromStream(stream, category);
    }

    private static bool IsParseFailure(Exception error) =>
        error is InvalidDataException
            or EndOfStreamException
            or IndexOutOfRangeException
            or ArgumentOutOfRangeException
            or KeyNotFoundException;

    private static RawEnvelope ReadEnvelope(byte[] bytes, GameCategory category)
    {
        var cursor = new PexCursor(bytes, category == GameCategory.Skyrim);
        if (cursor.ReadUInt32() != PexMagic)
        {
            throw new InvalidDataException("PEX magic does not match the requested game format.");
        }

        cursor.Skip(1 + 1 + 2 + 8);
        cursor.SkipPrependedString();
        cursor.SkipPrependedString();
        cursor.SkipPrependedString();

        var stringCount = cursor.ReadUInt16();
        var strings = new List<string>(stringCount);
        for (var index = 0; index < stringCount; index++)
        {
            strings.Add(DecodePexString(cursor.ReadPrependedBytes()));
        }

        return new RawEnvelope(
            strings,
            cursor.Position,
            bytes.AsSpan(cursor.Position).ToArray());
    }

    private static NormalizedPex NormalizeOfficialFallout4Layout(
        byte[] bytes,
        RawEnvelope envelope)
    {
        var cursor = new PexCursor(bytes, bigEndian: false) { Position = envelope.PayloadOffset };
        var hasDebugInfo = cursor.ReadByte();
        if (hasDebugInfo is not (0 or 1))
        {
            throw new InvalidDataException($"Unsupported Fallout 4 PEX debug-info flag: {hasDebugInfo}.");
        }

        var groups = new List<PexDebugPropertyGroupMetadata>();
        var removals = new List<ByteRange>();
        if (hasDebugInfo == 1)
        {
            cursor.Skip(8);
            var functionCount = cursor.ReadUInt16();
            for (var index = 0; index < functionCount; index++)
            {
                ReadStringIndex(cursor, envelope.Strings, "debug function object");
                ReadStringIndex(cursor, envelope.Strings, "debug function state");
                ReadStringIndex(cursor, envelope.Strings, "debug function name");
                cursor.Skip(1);
                cursor.Skip(checked(cursor.ReadUInt16() * 2));
            }

            var groupCount = cursor.ReadUInt16();
            for (var index = 0; index < groupCount; index++)
            {
                var objectName = ReadStringIndex(cursor, envelope.Strings, "debug property-group object");
                var groupName = ReadStringIndex(cursor, envelope.Strings, "debug property-group name");
                var compatibilityFieldsOffset = cursor.Position;
                var docString = ReadStringIndex(cursor, envelope.Strings, "debug property-group doc string");
                var userFlags = cursor.ReadUInt32();
                removals.Add(new ByteRange(compatibilityFieldsOffset, 6));

                var nameCount = cursor.ReadUInt16();
                var propertyNames = new List<string>(nameCount);
                for (var nameIndex = 0; nameIndex < nameCount; nameIndex++)
                {
                    propertyNames.Add(ReadStringIndex(
                        cursor,
                        envelope.Strings,
                        "debug property-group property name"));
                }
                groups.Add(new PexDebugPropertyGroupMetadata(
                    objectName,
                    groupName,
                    docString,
                    userFlags,
                    propertyNames));
            }

            var structOrderCount = cursor.ReadUInt16();
            for (var index = 0; index < structOrderCount; index++)
            {
                ReadStringIndex(cursor, envelope.Strings, "debug struct-order object");
                ReadStringIndex(cursor, envelope.Strings, "debug struct-order name");
                var nameCount = cursor.ReadUInt16();
                for (var nameIndex = 0; nameIndex < nameCount; nameIndex++)
                {
                    ReadStringIndex(cursor, envelope.Strings, "debug struct-order member");
                }
            }
        }

        var userFlagCount = cursor.ReadUInt16();
        for (var index = 0; index < userFlagCount; index++)
        {
            ReadStringIndex(cursor, envelope.Strings, "user flag");
            cursor.Skip(1);
        }

        var sizePatches = new List<ObjectSizePatch>();
        var objectCount = cursor.ReadUInt16();
        for (var index = 0; index < objectCount; index++)
        {
            ReadObject(cursor, envelope.Strings, removals, sizePatches);
        }
        if (cursor.Position != bytes.Length)
        {
            throw new InvalidDataException(
                $"Official Fallout 4 PEX parser did not reach end of stream: {cursor.Position}/{bytes.Length}.");
        }
        if (removals.Count == 0)
        {
            throw new InvalidDataException("PEX does not contain an official-layout field requiring compatibility normalization.");
        }

        using var output = new MemoryStream(bytes.Length - removals.Sum(item => item.Length));
        var sourceOffset = 0;
        foreach (var removal in removals)
        {
            output.Write(bytes, sourceOffset, removal.Offset - sourceOffset);
            sourceOffset = removal.Offset + removal.Length;
        }
        output.Write(bytes, sourceOffset, bytes.Length - sourceOffset);
        var normalizedBytes = output.ToArray();
        foreach (var patch in sizePatches)
        {
            var normalizedOffset = patch.Offset
                - removals.Where(item => item.Offset < patch.Offset).Sum(item => item.Length);
            BinaryPrimitives.WriteUInt32LittleEndian(
                normalizedBytes.AsSpan(normalizedOffset, 4),
                patch.NormalizedSize);
        }
        return new NormalizedPex(normalizedBytes, groups);
    }

    private static void ReadObject(
        PexCursor cursor,
        IReadOnlyList<string> strings,
        ICollection<ByteRange> removals,
        ICollection<ObjectSizePatch> sizePatches)
    {
        ReadStringIndex(cursor, strings, "object name");
        var sizeOffset = cursor.Position;
        var objectSize = cursor.ReadUInt32();
        if (objectSize < 4 || objectSize > int.MaxValue)
        {
            throw new InvalidDataException($"Invalid Fallout 4 PEX object size: {objectSize}.");
        }
        var contentStart = cursor.Position;
        var expectedEnd = checked(contentStart + (int)objectSize - 4);
        var removalCountBefore = removals.Count;

        ReadStringIndex(cursor, strings, "object parent class");
        ReadStringIndex(cursor, strings, "object doc string");
        cursor.Skip(1 + 4);
        ReadStringIndex(cursor, strings, "object auto state");

        var structCount = cursor.ReadUInt16();
        for (var index = 0; index < structCount; index++)
        {
            ReadStringIndex(cursor, strings, "struct name");
            var memberCount = cursor.ReadUInt16();
            for (var memberIndex = 0; memberIndex < memberCount; memberIndex++)
            {
                ReadStringIndex(cursor, strings, "struct member name");
                ReadStringIndex(cursor, strings, "struct member type");
                cursor.Skip(4);
                ReadVariableData(cursor, strings);
                cursor.Skip(1);
                ReadStringIndex(cursor, strings, "struct member doc string");
            }
        }

        var variableCount = cursor.ReadUInt16();
        for (var index = 0; index < variableCount; index++)
        {
            ReadStringIndex(cursor, strings, "variable name");
            ReadStringIndex(cursor, strings, "variable type");
            cursor.Skip(4);
            ReadVariableData(cursor, strings);
            removals.Add(new ByteRange(cursor.Position, 1));
            cursor.Skip(1);
        }

        var propertyCount = cursor.ReadUInt16();
        for (var index = 0; index < propertyCount; index++)
        {
            ReadStringIndex(cursor, strings, "property name");
            ReadStringIndex(cursor, strings, "property type");
            ReadStringIndex(cursor, strings, "property doc string");
            cursor.Skip(4);
            var flags = cursor.ReadByte();
            if ((flags & 4) != 0)
            {
                ReadStringIndex(cursor, strings, "property auto variable");
            }
            if ((flags & 5) == 1)
            {
                ReadFunction(cursor, strings);
            }
            if ((flags & 6) == 2)
            {
                ReadFunction(cursor, strings);
            }
        }

        var stateCount = cursor.ReadUInt16();
        for (var index = 0; index < stateCount; index++)
        {
            ReadStringIndex(cursor, strings, "state name");
            var functionCount = cursor.ReadUInt16();
            for (var functionIndex = 0; functionIndex < functionCount; functionIndex++)
            {
                ReadStringIndex(cursor, strings, "function name");
                ReadFunction(cursor, strings);
            }
        }

        if (cursor.Position != expectedEnd)
        {
            throw new InvalidDataException(
                $"Fallout 4 PEX object length mismatch: {cursor.Position}/{expectedEnd}.");
        }
        var removedBytes = removals.Skip(removalCountBefore).Sum(item => item.Length);
        sizePatches.Add(new ObjectSizePatch(sizeOffset, checked(objectSize - (uint)removedBytes)));
    }

    private static void ReadFunction(PexCursor cursor, IReadOnlyList<string> strings)
    {
        ReadStringIndex(cursor, strings, "function return type");
        ReadStringIndex(cursor, strings, "function doc string");
        cursor.Skip(4 + 1);
        ReadTypedNames(cursor, strings, "function parameter");
        ReadTypedNames(cursor, strings, "function local");

        var instructionCount = cursor.ReadUInt16();
        for (var index = 0; index < instructionCount; index++)
        {
            var opcode = (InstructionOpcode)cursor.ReadByte();
            var argumentShape = InstructionOpCodeArguments.GetArguments(opcode);
            foreach (var argument in argumentShape)
            {
                var value = ReadVariableData(cursor, strings);
                if (argument != '*')
                {
                    continue;
                }
                if (value.Type != (byte)VariableType.Integer || value.IntegerValue is null or < 0)
                {
                    throw new InvalidDataException("PEX variable-length instruction has an invalid count argument.");
                }
                for (var extraIndex = 0; extraIndex < value.IntegerValue.Value; extraIndex++)
                {
                    ReadVariableData(cursor, strings);
                }
            }
        }
    }

    private static void ReadTypedNames(
        PexCursor cursor,
        IReadOnlyList<string> strings,
        string role)
    {
        var count = cursor.ReadUInt16();
        for (var index = 0; index < count; index++)
        {
            ReadStringIndex(cursor, strings, $"{role} name");
            ReadStringIndex(cursor, strings, $"{role} type");
        }
    }

    private static RawVariableData ReadVariableData(
        PexCursor cursor,
        IReadOnlyList<string> strings)
    {
        var type = cursor.ReadByte();
        return type switch
        {
            (byte)VariableType.Null => new RawVariableData(type, null),
            (byte)VariableType.Identifier or (byte)VariableType.String =>
                ReadStringVariable(cursor, strings, type),
            (byte)VariableType.Integer => new RawVariableData(type, unchecked((int)cursor.ReadUInt32())),
            (byte)VariableType.Float => SkipVariable(cursor, type, 4),
            (byte)VariableType.Bool => SkipVariable(cursor, type, 1),
            _ => throw new InvalidDataException($"Unsupported PEX variable type: {type}."),
        };
    }

    private static RawVariableData ReadStringVariable(
        PexCursor cursor,
        IReadOnlyList<string> strings,
        byte type)
    {
        ReadStringIndex(cursor, strings, "variable value");
        return new RawVariableData(type, null);
    }

    private static RawVariableData SkipVariable(PexCursor cursor, byte type, int bytes)
    {
        cursor.Skip(bytes);
        return new RawVariableData(type, null);
    }

    private static void ValidateNormalizedGroups(
        PexFile file,
        IReadOnlyList<PexDebugPropertyGroupMetadata> expected)
    {
        if (expected.Count == 0)
        {
            if (file.DebugInfo?.PropertyGroups.Count > 0)
            {
                throw new InvalidDataException(
                    "Normalized Fallout 4 PEX gained unexpected debug property groups.");
            }
            return;
        }

        var actual = file.DebugInfo?.PropertyGroups
            ?? throw new InvalidDataException("Normalized Fallout 4 PEX lost debug property groups.");
        if (actual.Count != expected.Count)
        {
            throw new InvalidDataException("Normalized Fallout 4 debug property-group count changed.");
        }

        for (var index = 0; index < expected.Count; index++)
        {
            if (!string.Equals(actual[index].ObjectName, expected[index].ObjectName, StringComparison.Ordinal)
                || !string.Equals(actual[index].GroupName, expected[index].GroupName, StringComparison.Ordinal)
                || !actual[index].PropertyNames.SequenceEqual(expected[index].PropertyNames, StringComparer.Ordinal))
            {
                throw new InvalidDataException(
                    $"Normalized Fallout 4 debug property group {index} changed identity.");
            }
        }
    }

    private static string ReadStringIndex(
        PexCursor cursor,
        IReadOnlyList<string> strings,
        string role)
    {
        var index = cursor.ReadUInt16();
        if (index >= strings.Count)
        {
            throw new InvalidDataException(
                $"PEX {role} string index {index} exceeds string table count {strings.Count}.");
        }
        return strings[index];
    }

    private static string DecodePexString(byte[] bytes) =>
        Encoding.GetEncoding(1252).GetString(bytes);

    private sealed record RawEnvelope(
        IReadOnlyList<string> Strings,
        int PayloadOffset,
        byte[] PostStringTablePayload);

    private sealed record NormalizedPex(
        byte[] Bytes,
        IReadOnlyList<PexDebugPropertyGroupMetadata> PropertyGroups);

    private readonly record struct ByteRange(int Offset, int Length);

    private readonly record struct ObjectSizePatch(int Offset, uint NormalizedSize);

    private readonly record struct RawVariableData(byte Type, int? IntegerValue);

    private sealed class PexCursor(byte[] bytes, bool bigEndian)
    {
        public int Position { get; set; }

        public byte ReadByte()
        {
            Require(1);
            return bytes[Position++];
        }

        public ushort ReadUInt16()
        {
            Require(2);
            var value = bigEndian
                ? BinaryPrimitives.ReadUInt16BigEndian(bytes.AsSpan(Position, 2))
                : BinaryPrimitives.ReadUInt16LittleEndian(bytes.AsSpan(Position, 2));
            Position += 2;
            return value;
        }

        public uint ReadUInt32()
        {
            Require(4);
            var value = bigEndian
                ? BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(Position, 4))
                : BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(Position, 4));
            Position += 4;
            return value;
        }

        public void Skip(int count)
        {
            Require(count);
            Position += count;
        }

        public void SkipPrependedString() => Skip(ReadUInt16());

        public byte[] ReadPrependedBytes()
        {
            var length = ReadUInt16();
            Require(length);
            var value = bytes.AsSpan(Position, length).ToArray();
            Position += length;
            return value;
        }

        private void Require(int count)
        {
            if (count < 0 || Position < 0 || Position > bytes.Length - count)
            {
                throw new InvalidDataException("Unexpected end of PEX stream.");
            }
        }
    }
}
