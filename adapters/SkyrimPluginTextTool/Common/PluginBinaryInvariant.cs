using System.Buffers.Binary;
using System.Globalization;
using System.IO.Compression;
using System.Text;

internal sealed record PluginBinaryInvariantResult(
    bool Verified,
    int RecordsChecked,
    int TargetsVerified,
    string[] AllowedHeaderChanges,
    string[] Issues);

internal sealed record PluginRawSubrecord(
    string RecordType,
    uint RawFormId,
    string SubrecordType,
    int SubrecordIndex,
    byte[] Payload);

internal static class PluginBinaryInvariant
{
    private const uint CompressedRecordFlag = 0x00040000;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);
    private static readonly UTF8Encoding ReplacementUtf8 = new(false, false);
    private static readonly Encoding StrictCp1252;

    static PluginBinaryInvariant()
    {
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        StrictCp1252 = Encoding.GetEncoding(
            1252,
            EncoderFallback.ExceptionFallback,
            DecoderFallback.ExceptionFallback);
    }

    public static PluginBinaryInvariantResult Verify(
        string inputPlugin,
        string outputPlugin,
        IReadOnlyCollection<TranslationRow> rows)
    {
        var issues = new List<string>();
        var allowedChanges = new List<string>();
        var targets = BuildTargets(rows, issues);
        BinarySnapshot input;
        BinarySnapshot output;
        try
        {
            input = BinarySnapshot.Read(inputPlugin);
            output = BinarySnapshot.Read(outputPlugin);
        }
        catch (Exception exc)
        {
            issues.Add($"binary snapshot parse failed: {exc.Message}");
            return new(false, 0, 0, [], issues.ToArray());
        }

        if (input.Elements.Count != output.Elements.Count)
        {
            issues.Add($"element count changed: {input.Elements.Count} -> {output.Elements.Count}");
        }
        var matchedTargets = new HashSet<TargetKey>();
        var pairCount = Math.Min(input.Elements.Count, output.Elements.Count);
        var recordsChecked = 0;
        for (var index = 0; index < pairCount; index++)
        {
            var left = input.Elements[index];
            var right = output.Elements[index];
            if (left.Kind != right.Kind || left.Signature != right.Signature || left.Depth != right.Depth)
            {
                issues.Add($"element structure changed at index {index}: {left.Describe()} -> {right.Describe()}");
                continue;
            }
            if (left.Kind == ElementKind.Group)
            {
                var groupHasTarget = input.Elements
                    .Skip(index + 1)
                    .TakeWhile(element => element.Depth > left.Depth)
                    .Any(element =>
                        element.Kind == ElementKind.Record
                        && targets.Keys.Any(key => key.RecordType == element.Signature && key.FormId == element.FormId));
                CompareGroupHeader(left, right, index, groupHasTarget, issues, allowedChanges);
                continue;
            }

            recordsChecked++;
            if (left.FormId != right.FormId)
            {
                issues.Add($"record FormID changed at index {index}: {left.FormId:X8} -> {right.FormId:X8}");
                continue;
            }
            var recordHasTarget = targets.Keys.Any(key => key.RecordType == left.Signature && key.FormId == left.FormId);
            CompareRecordHeader(left, right, index, recordHasTarget, issues, allowedChanges);
            CompareSubrecords(left, right, targets, matchedTargets, issues);
        }

        foreach (var target in targets.Keys)
        {
            if (!matchedTargets.Contains(target))
            {
                issues.Add($"translation target was not verified exactly once: {target}");
            }
        }
        return new(
            issues.Count == 0,
            recordsChecked,
            matchedTargets.Count,
            allowedChanges.Distinct(StringComparer.Ordinal).ToArray(),
            issues.ToArray());
    }

    internal static IReadOnlyList<PluginRawSubrecord> ReadRawSubrecords(string pluginPath)
    {
        return BinarySnapshot.Read(pluginPath).Elements
            .Where(static element => element.Kind == ElementKind.Record)
            .SelectMany(static element => element.Subrecords.Select(subrecord => new PluginRawSubrecord(
                element.Signature,
                element.FormId,
                subrecord.Signature,
                subrecord.Index,
                subrecord.Payload)))
            .ToArray();
    }

    internal static IReadOnlyList<uint> ReadRawMajorRecordFormIds(string pluginPath)
    {
        return BinarySnapshot.Read(pluginPath).Elements
            .Where(static element =>
                element.Kind == ElementKind.Record
                && !string.Equals(element.Signature, "TES4", StringComparison.Ordinal))
            .Select(static element => element.FormId)
            .ToArray();
    }

    internal static string DecodeSourcePayload(byte[] payload)
    {
        var contentLength = payload.Length - TrailingNullCount(payload);
        var content = payload.AsSpan(0, contentLength).ToArray();
        try
        {
            return StrictUtf8.GetString(content);
        }
        catch (DecoderFallbackException)
        {
            if (content.Any(static value => value is 0x81 or 0x8D or 0x8F or 0x90 or 0x9D))
            {
                return ReplacementUtf8.GetString(content);
            }
            return StrictCp1252.GetString(content);
        }
    }

    internal static bool IsStrictUtf8Payload(byte[] payload)
    {
        var contentLength = payload.Length - TrailingNullCount(payload);
        try
        {
            _ = StrictUtf8.GetString(payload, 0, contentLength);
            return true;
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
    }

    private static Dictionary<TargetKey, TranslationRow> BuildTargets(
        IEnumerable<TranslationRow> rows,
        List<string> issues)
    {
        var targets = new Dictionary<TargetKey, TranslationRow>();
        foreach (var row in rows)
        {
            if (!TryParseFormId(row.FormId, out var formId))
            {
                issues.Add($"invalid target FormID: {row.FormId}");
                continue;
            }
            var key = new TargetKey(row.RecordType, formId, row.SubrecordType, row.SubrecordIndex);
            if (!targets.TryAdd(key, row))
            {
                issues.Add($"duplicate translation target: {key}");
            }
        }
        return targets;
    }

    private static void CompareGroupHeader(
        ElementSnapshot input,
        ElementSnapshot output,
        int index,
        bool groupHasTarget,
        List<string> issues,
        List<string> allowedChanges)
    {
        if (!input.Header.AsSpan(0, 4).SequenceEqual(output.Header.AsSpan(0, 4))
            || !input.Header.AsSpan(8).SequenceEqual(output.Header.AsSpan(8)))
        {
            issues.Add($"GRUP header changed outside allowed size field at element {index}");
        }
        if (!input.Header.AsSpan(4, 4).SequenceEqual(output.Header.AsSpan(4, 4)))
        {
            if (groupHasTarget)
            {
                allowedChanges.Add("GRUP header bytes 4..7 (group size for translated descendant payload)");
            }
            else
            {
                issues.Add($"non-target GRUP size changed at element {index}");
            }
        }
    }

    private static void CompareRecordHeader(
        ElementSnapshot input,
        ElementSnapshot output,
        int index,
        bool recordHasTarget,
        List<string> issues,
        List<string> allowedChanges)
    {
        if (!recordHasTarget)
        {
            if (!input.Header.SequenceEqual(output.Header))
            {
                issues.Add($"non-target record header changed at element {index} ({input.Signature} {input.FormId:X8})");
            }
            return;
        }
        if (!input.Header.AsSpan(0, 4).SequenceEqual(output.Header.AsSpan(0, 4))
            || !input.Header.AsSpan(8).SequenceEqual(output.Header.AsSpan(8)))
        {
            issues.Add($"target record header changed outside allowed data-size field at element {index} ({input.Signature} {input.FormId:X8})");
        }
        if (!input.Header.AsSpan(4, 4).SequenceEqual(output.Header.AsSpan(4, 4)))
        {
            allowedChanges.Add("target record header bytes 4..7 (record data size)");
        }
    }

    private static void CompareSubrecords(
        ElementSnapshot input,
        ElementSnapshot output,
        IReadOnlyDictionary<TargetKey, TranslationRow> targets,
        HashSet<TargetKey> matchedTargets,
        List<string> issues)
    {
        if (input.Subrecords.Count != output.Subrecords.Count)
        {
            issues.Add($"subrecord count changed for {input.Signature} {input.FormId:X8}: {input.Subrecords.Count} -> {output.Subrecords.Count}");
        }
        var count = Math.Min(input.Subrecords.Count, output.Subrecords.Count);
        for (var index = 0; index < count; index++)
        {
            var left = input.Subrecords[index];
            var right = output.Subrecords[index];
            if (left.Signature != right.Signature || left.Index != right.Index)
            {
                issues.Add($"subrecord type/order/index changed for {input.Signature} {input.FormId:X8} at index {index}");
                continue;
            }
            var key = new TargetKey(input.Signature, input.FormId, left.Signature, left.Index);
            if (!targets.TryGetValue(key, out var row))
            {
                if (!left.Payload.SequenceEqual(right.Payload))
                {
                    issues.Add($"non-target payload changed for {key}");
                }
                continue;
            }
            if (!TextPayloadEquals(left.Payload, row.Source, requireUtf8: false))
            {
                issues.Add($"target source payload does not exactly match translation row for {key}");
                continue;
            }
            if (!TextPayloadEquals(right.Payload, row.Target, requireUtf8: true))
            {
                issues.Add($"target output payload does not exactly match translation row for {key}");
                continue;
            }
            if (TrailingNullCount(left.Payload) != TrailingNullCount(right.Payload))
            {
                issues.Add($"target payload terminator changed for {key}");
                continue;
            }
            matchedTargets.Add(key);
        }
    }

    private static bool TextPayloadEquals(byte[] payload, string expected, bool requireUtf8)
    {
        var contentLength = payload.Length - TrailingNullCount(payload);
        var content = payload.AsSpan(0, contentLength).ToArray();
        if (!requireUtf8)
        {
            return DecodeSourcePayload(payload) == expected;
        }
        try
        {
            return StrictUtf8.GetString(content) == expected;
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
    }

    private static int TrailingNullCount(byte[] payload)
    {
        var count = 0;
        for (var index = payload.Length - 1; index >= 0 && payload[index] == 0; index--) count++;
        return count;
    }

    private static bool TryParseFormId(string value, out uint formId)
    {
        var text = (value ?? string.Empty).Trim();
        if (text.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) text = text[2..];
        return uint.TryParse(text, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out formId);
    }

    private enum ElementKind { Group, Record }

    private sealed record TargetKey(string RecordType, uint FormId, string SubrecordType, int SubrecordIndex)
    {
        public override string ToString() => $"{RecordType} {FormId:X8} {SubrecordType}[{SubrecordIndex}]";
    }

    private sealed record SubrecordSnapshot(string Signature, int Index, byte[] Payload);

    private sealed record ElementSnapshot(
        ElementKind Kind,
        string Signature,
        int Depth,
        byte[] Header,
        uint FormId,
        List<SubrecordSnapshot> Subrecords)
    {
        public string Describe() => Kind == ElementKind.Group ? $"GRUP/{Signature}@{Depth}" : $"{Signature}/{FormId:X8}@{Depth}";
    }

    private sealed record BinarySnapshot(List<ElementSnapshot> Elements)
    {
        public static BinarySnapshot Read(string path)
        {
            var bytes = File.ReadAllBytes(path);
            var elements = new List<ElementSnapshot>();
            ParseElements(bytes, 0, bytes.Length, 0, elements);
            return new BinarySnapshot(elements);
        }

        private static void ParseElements(byte[] bytes, int start, int end, int depth, List<ElementSnapshot> elements)
        {
            var offset = start;
            while (offset < end)
            {
                if (offset + 24 > end) throw new InvalidDataException($"truncated element header at 0x{offset:X}");
                var signature = Encoding.ASCII.GetString(bytes, offset, 4);
                if (signature == "GRUP")
                {
                    var size = checked((int)BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset + 4, 4)));
                    if (size < 24 || offset + size > end) throw new InvalidDataException($"invalid GRUP size at 0x{offset:X}");
                    var label = Convert.ToHexString(bytes.AsSpan(offset + 8, 4));
                    elements.Add(new(ElementKind.Group, label, depth, bytes.AsSpan(offset, 24).ToArray(), 0, []));
                    ParseElements(bytes, offset + 24, offset + size, depth + 1, elements);
                    offset += size;
                    continue;
                }
                if (!signature.All(character => char.IsAsciiLetterUpper(character) || char.IsAsciiDigit(character)))
                {
                    throw new InvalidDataException($"invalid record signature at 0x{offset:X}");
                }
                var dataSize = checked((int)BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset + 4, 4)));
                var dataStart = offset + 24;
                if (dataSize < 0 || dataStart + dataSize > end) throw new InvalidDataException($"invalid record size at 0x{offset:X}");
                var flags = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset + 8, 4));
                var formId = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset + 12, 4));
                var recordData = bytes.AsSpan(dataStart, dataSize).ToArray();
                if ((flags & CompressedRecordFlag) != 0)
                {
                    if (recordData.Length < 4) throw new InvalidDataException($"compressed record missing size at 0x{offset:X}");
                    var expectedSize = checked((int)BinaryPrimitives.ReadUInt32LittleEndian(recordData.AsSpan(0, 4)));
                    using var input = new MemoryStream(recordData, 4, recordData.Length - 4, writable: false);
                    using var zlib = new ZLibStream(input, CompressionMode.Decompress);
                    using var output = new MemoryStream();
                    zlib.CopyTo(output);
                    recordData = output.ToArray();
                    if (recordData.Length != expectedSize) throw new InvalidDataException($"compressed record size mismatch at 0x{offset:X}");
                }
                elements.Add(new(
                    ElementKind.Record,
                    signature,
                    depth,
                    bytes.AsSpan(offset, 24).ToArray(),
                    formId,
                    ParseSubrecords(recordData, offset)));
                offset = dataStart + dataSize;
            }
        }

        private static List<SubrecordSnapshot> ParseSubrecords(byte[] data, int recordOffset)
        {
            var rows = new List<SubrecordSnapshot>();
            var offset = 0;
            uint? extendedSize = null;
            var index = 0;
            while (offset < data.Length)
            {
                if (offset + 6 > data.Length) throw new InvalidDataException($"truncated subrecord in record 0x{recordOffset:X}");
                var signature = Encoding.ASCII.GetString(data, offset, 4);
                var size = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 4, 2));
                offset += 6;
                if (signature == "XXXX")
                {
                    if (size != 4 || offset + 4 > data.Length) throw new InvalidDataException($"invalid XXXX subrecord in record 0x{recordOffset:X}");
                    extendedSize = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));
                    offset += 4;
                    continue;
                }
                var payloadSize = checked((int)(extendedSize ?? size));
                extendedSize = null;
                if (payloadSize < 0 || offset + payloadSize > data.Length) throw new InvalidDataException($"invalid subrecord size in record 0x{recordOffset:X}");
                rows.Add(new(signature, index, data.AsSpan(offset, payloadSize).ToArray()));
                offset += payloadSize;
                index++;
            }
            if (extendedSize is not null) throw new InvalidDataException($"orphan XXXX subrecord in record 0x{recordOffset:X}");
            return rows;
        }
    }
}
