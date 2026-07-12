using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Strings;

internal sealed record Fallout4PluginExportRow(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("game_id")] string GameId,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("plugin")] string Plugin,
    [property: JsonPropertyName("record_type")] string RecordType,
    [property: JsonPropertyName("form_id")] string FormId,
    [property: JsonPropertyName("editor_id")] string EditorId,
    [property: JsonPropertyName("field_path")] string FieldPath,
    [property: JsonPropertyName("group_path")] string GroupPath,
    [property: JsonPropertyName("record_offset")] int RecordOffset,
    [property: JsonPropertyName("subrecord_type")] string SubrecordType,
    [property: JsonPropertyName("subrecord_index")] int SubrecordIndex,
    [property: JsonPropertyName("payload_size")] int PayloadSize,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("target")] string Target,
    [property: JsonPropertyName("risk")] string Risk,
    [property: JsonPropertyName("writeback")] string Writeback,
    [property: JsonPropertyName("reason")] string Reason);

internal static class Fallout4PluginExporter
{
    private static readonly HashSet<(string RecordType, string SubrecordType)> SupportedFields =
    [
        ("WEAP", "FULL"),
        ("ARMO", "FULL"),
        ("MISC", "FULL"),
        ("ALCH", "FULL"),
        ("CELL", "FULL"),
        ("WRLD", "FULL"),
        ("PERK", "FULL"),
        ("PERK", "DESC"),
        ("MGEF", "FULL"),
        ("MGEF", "DNAM"),
        ("SPEL", "FULL"),
        ("SPEL", "DESC"),
        ("MESG", "DESC"),
        ("QUST", "FULL"),
    ];

    public static IReadOnlyList<Fallout4PluginExportRow> Export(
        string inputPlugin,
        string relativeInputPath)
    {
        if (Fallout4PluginAdapter.IsLocalized(inputPlugin))
        {
            throw new InvalidOperationException(
                "Fallout 4 localized plugin requires an unavailable string-table adapter.");
        }

        var rawSubrecords = PluginBinaryInvariant.ReadRawSubrecords(inputPlugin);
        var supportedRaw = rawSubrecords
            .Where(raw => SupportedFields.Contains((raw.RecordType, raw.SubrecordType)))
            .ToArray();
        var hasUtf8NonAscii = supportedRaw.Any(raw =>
            raw.Payload.Any(static value => value >= 0x80) && PluginBinaryInvariant.IsStrictUtf8Payload(raw.Payload));
        var hasLegacyNonAscii = supportedRaw.Any(raw =>
            raw.Payload.Any(static value => value >= 0x80) && !PluginBinaryInvariant.IsStrictUtf8Payload(raw.Payload));
        if (hasUtf8NonAscii && hasLegacyNonAscii)
        {
            throw new InvalidDataException("mixed UTF-8 and legacy non-ASCII plugin strings are not supported");
        }
        System.Text.Encoding.RegisterProvider(System.Text.CodePagesEncodingProvider.Instance);
        var encoding = hasUtf8NonAscii
            ? new System.Text.UTF8Encoding(false, true)
            : System.Text.Encoding.GetEncoding(1252, System.Text.EncoderFallback.ExceptionFallback, System.Text.DecoderFallback.ExceptionFallback);
        var mutagenEncoding = new MutagenEncodingWrapper(encoding);
        var readParameters = new BinaryReadParameters
        {
            StringsParam = new StringsReadParameters
            {
                NonLocalizedEncodingOverride = mutagenEncoding,
                NonTranslatedEncodingOverride = mutagenEncoding,
            },
        };
        var mod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4, readParameters);
        var resolver = new PluginFormKeyResolver(mod);
        var fields = BuildFields(mod);
        var rows = new List<Fallout4PluginExportRow>();
        foreach (var raw in rawSubrecords)
        {
            if (!SupportedFields.Contains((raw.RecordType, raw.SubrecordType))) continue;
            if (!resolver.TryResolve(raw.RawFormId.ToString("X8"), out var formKey, out var reason))
            {
                throw new InvalidDataException(
                    $"Mutagen export could not resolve {raw.RecordType} {raw.RawFormId:X8}: {reason}");
            }
            var key = new FieldKey(raw.RecordType, formKey, raw.SubrecordType);
            if (!fields.TryGetValue(key, out var field))
            {
                throw new InvalidDataException(
                    $"Mutagen export could not bind {raw.RecordType} {raw.RawFormId:X8} {raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            var rawSource = PluginBinaryInvariant.DecodeSourcePayload(raw.Payload);
            if (!string.Equals(rawSource, field.Source, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    $"Mutagen/raw source mismatch for {raw.RecordType} {raw.RawFormId:X8} {raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            if (string.IsNullOrWhiteSpace(field.Source)) continue;
            rows.Add(new(
                2,
                "fallout4",
                relativeInputPath,
                Path.GetFileName(inputPlugin),
                raw.RecordType,
                raw.RawFormId.ToString("X8"),
                field.EditorId,
                field.FieldPath,
                string.Empty,
                0,
                raw.SubrecordType,
                raw.SubrecordIndex,
                raw.Payload.Length,
                field.Source,
                string.Empty,
                "candidate",
                "supported",
                "fallout4_mutagen_supported_field"));
        }
        return rows;
    }

    public static void WriteJsonl(string outputJsonl, IReadOnlyList<Fallout4PluginExportRow> rows)
    {
        AtomicPluginOutput.PrepareTarget(outputJsonl);
        var temporary = AtomicPluginOutput.CreateTemporaryPath(outputJsonl);
        try
        {
            using (var writer = new StreamWriter(temporary, false, new System.Text.UTF8Encoding(false)))
            {
                foreach (var row in rows)
                {
                    writer.WriteLine(JsonSerializer.Serialize(row));
                }
            }
            AtomicPluginOutput.Commit(temporary, outputJsonl);
        }
        catch
        {
            AtomicPluginOutput.CleanupFailure(temporary, outputJsonl);
            throw;
        }
    }

    private static Dictionary<FieldKey, ExportField> BuildFields(Fallout4Mod mod)
    {
        var fields = new Dictionary<FieldKey, ExportField>();
        Add(fields, mod.Weapons, "WEAP", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Armors, "ARMO", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.MiscItems, "MISC", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Ingestibles, "ALCH", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, EnumerateCells(mod), "CELL", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Worldspaces, "WRLD", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Perks, "PERK", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Perks, "PERK", "DESC", "Description", static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "");
        Add(fields, mod.MagicEffects, "MGEF", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.MagicEffects, "MGEF", "DNAM", "Description", static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "");
        Add(fields, mod.Spells, "SPEL", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        Add(fields, mod.Spells, "SPEL", "DESC", "Description", static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "");
        Add(fields, mod.Messages, "MESG", "DESC", "Description", static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "");
        Add(fields, mod.Quests, "QUST", "FULL", "Name", static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "");
        return fields;
    }

    private static void Add<TRecord>(
        IDictionary<FieldKey, ExportField> fields,
        IEnumerable<TRecord> records,
        string recordType,
        string subrecordType,
        string fieldPath,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source)
    {
        foreach (var record in records)
        {
            var value = source(record);
            if (string.IsNullOrWhiteSpace(value)) continue;
            var key = new FieldKey(recordType, formKey(record), subrecordType);
            if (!fields.TryAdd(key, new(fieldPath, editorId(record) ?? string.Empty, value)))
            {
                throw new InvalidDataException($"duplicate Mutagen export identity: {key}");
            }
        }
    }

    private static IEnumerable<Cell> EnumerateCells(Fallout4Mod mod)
    {
        foreach (var block in mod.Cells.Records)
        foreach (var subBlock in block.SubBlocks)
        foreach (var cell in subBlock.Cells)
            yield return cell;
    }

    private sealed record FieldKey(string RecordType, FormKey FormKey, string SubrecordType);
    private sealed record ExportField(string FieldPath, string EditorId, string Source);
}
