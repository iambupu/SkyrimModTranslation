using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Skyrim;
using Mutagen.Bethesda.Strings;

internal sealed record SkyrimPluginExportRow(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("game_id")] string GameId,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("plugin")] string Plugin,
    [property: JsonPropertyName("record_type")] string RecordType,
    [property: JsonPropertyName("form_id")] string FormId,
    [property: JsonPropertyName("owner_mod_key"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? OwnerModKey,
    [property: JsonPropertyName("local_id"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] uint? LocalId,
    [property: JsonPropertyName("master_style"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? MasterStyle,
    [property: JsonPropertyName("master_style_evidence"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? MasterStyleEvidence,
    [property: JsonPropertyName("editor_id")] string EditorId,
    [property: JsonPropertyName("field_path")] string FieldPath,
    [property: JsonPropertyName("group_path")] string GroupPath,
    [property: JsonPropertyName("record_offset")] int RecordOffset,
    [property: JsonPropertyName("subrecord_type")] string SubrecordType,
    [property: JsonPropertyName("subrecord_index")] int SubrecordIndex,
    [property: JsonPropertyName("occurrence_index")] int? OccurrenceIndex,
    [property: JsonPropertyName("payload_size")] int PayloadSize,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("target")] string Target,
    [property: JsonPropertyName("risk")] string Risk,
    [property: JsonPropertyName("writeback")] string Writeback,
    [property: JsonPropertyName("reason")] string Reason);

internal sealed record SkyrimPluginExportResult(
    IReadOnlyList<SkyrimPluginExportRow> Rows,
    PluginTraits Traits,
    string BlockedReason,
    string MasterStyleContextPath)
{
    public bool Blocked => !string.IsNullOrWhiteSpace(BlockedReason);
}

internal static class SkyrimPluginExporter
{
    public static LocalizedPluginReferenceInventoryResult InventoryLocalizedReferences(
        string projectRoot,
        string inputPlugin,
        string relativeInputPath,
        string? masterStyleManifest)
    {
        var masterContext = PluginMasterStyleContext.Resolve(
            projectRoot,
            inputPlugin,
            "skyrim-se",
            masterStyleManifest);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
        var readParameters = new BinaryReadParameters
        {
            MasterFlagsLookup = masterContext.MasterFlagsLookup,
        };
        var mod = SkyrimMod.CreateFromBinary(
            inputPlugin,
            SkyrimRelease.SkyrimSE,
            readParameters);
        var traits = SkyrimPluginTraits.Inspect(inputPlugin, mod, majorRecordFormIds);
        if (masterContext.Required)
        {
            traits = traits with { ContainsUnsupportedLightFormIds = false };
        }
        return LocalizedPluginReferenceInventory.Read(
            "skyrim-se",
            inputPlugin,
            relativeInputPath,
            mod,
            traits,
            masterContext);
    }

    public static SkyrimPluginExportResult Export(
        string projectRoot,
        string inputPlugin,
        string relativeInputPath,
        string? masterStyleManifest)
    {
        var masterContext = PluginMasterStyleContext.Resolve(
            projectRoot,
            inputPlugin,
            "skyrim-se",
            masterStyleManifest);
        var rawSubrecords = PluginBinaryInvariant.ReadRawSubrecords(inputPlugin);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
        var supportedRaw = rawSubrecords
            .Where(raw => PluginFieldContract.TryGetFieldPath(
                "skyrim-se",
                raw.RecordType,
                raw.SubrecordType,
                out _))
            .ToArray();
        var encoding = DetectEncoding(supportedRaw);
        var readParameters = new BinaryReadParameters
        {
            MasterFlagsLookup = masterContext.MasterFlagsLookup,
            StringsParam = new StringsReadParameters
            {
                NonLocalizedEncodingOverride = new MutagenEncodingWrapper(encoding),
                NonTranslatedEncodingOverride = new MutagenEncodingWrapper(encoding),
            },
        };
        var mod = SkyrimMod.CreateFromBinary(
            inputPlugin,
            SkyrimRelease.SkyrimSE,
            readParameters);
        var traits = SkyrimPluginTraits.Inspect(inputPlugin, mod, majorRecordFormIds);
        if (masterContext.Required)
        {
            traits = traits with { ContainsUnsupportedLightFormIds = false };
        }
        if (traits.Localized == true)
        {
            return new([], traits, string.Empty, masterContext.ContextPath);
        }

        var resolver = new PluginFormKeyResolver(mod, masterContext);
        var fields = BuildFields(mod);
        var occurrences = new Dictionary<RawFieldKey, int>();
        var rows = new List<SkyrimPluginExportRow>();
        foreach (var raw in rawSubrecords)
        {
            if (!PluginFieldContract.TryGetFieldPath(
                    "skyrim-se",
                    raw.RecordType,
                    raw.SubrecordType,
                    out var fieldPath))
            {
                continue;
            }
            if (!resolver.TryResolve(
                    raw.RawFormId.ToString("X8"),
                    out var formKey,
                    out var canonicalIdentity,
                    out var reason))
            {
                throw new InvalidDataException(
                    $"Skyrim export could not resolve {raw.RecordType} {raw.RawFormId:X8}: {reason}");
            }

            var rawKey = new RawFieldKey(raw.RecordType, raw.RawFormId, raw.SubrecordType);
            occurrences.TryGetValue(rawKey, out var occurrenceIndex);
            occurrences[rawKey] = occurrenceIndex + 1;
            var rawSource = PluginBinaryInvariant.DecodeSourcePayload(raw.Payload);
            if (string.IsNullOrWhiteSpace(rawSource)) continue;
            var fieldKey = new FieldKey(raw.RecordType, formKey, raw.SubrecordType, occurrenceIndex);
            if (!fields.TryGetValue(fieldKey, out var field))
            {
                throw new InvalidDataException(
                    $"Skyrim export could not bind {raw.RecordType} {raw.RawFormId:X8} "
                    + $"{raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            if (!string.Equals(rawSource, field.Source, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    $"Skyrim Mutagen/raw source mismatch for {raw.RecordType} "
                    + $"{raw.RawFormId:X8} {raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            rows.Add(new(
                2,
                "skyrim-se",
                relativeInputPath,
                Path.GetFileName(inputPlugin),
                raw.RecordType,
                raw.RawFormId.ToString("X8"),
                canonicalIdentity.RequiresCanonicalRow
                    ? canonicalIdentity.FormKey.ModKey.FileName.String
                    : null,
                canonicalIdentity.RequiresCanonicalRow
                    ? canonicalIdentity.LocalId
                    : null,
                canonicalIdentity.RequiresCanonicalRow
                    ? canonicalIdentity.MasterStyle
                    : null,
                canonicalIdentity.RequiresCanonicalRow
                    ? canonicalIdentity.EvidenceSource
                    : null,
                field.EditorId,
                fieldPath,
                string.Empty,
                0,
                raw.SubrecordType,
                raw.SubrecordIndex,
                PluginFieldContract.RequiresOccurrenceIndex(
                    "skyrim-se",
                    raw.RecordType,
                    raw.SubrecordType)
                    ? occurrenceIndex
                    : null,
                raw.Payload.Length,
                field.Source,
                string.Empty,
                "candidate",
                "supported",
                "skyrim_mutagen_supported_field"));
        }
        return new(rows, traits, string.Empty, masterContext.ContextPath);
    }

    public static void WriteJsonl(
        string outputJsonl,
        IReadOnlyList<SkyrimPluginExportRow> rows)
    {
        AtomicPluginOutput.PrepareTarget(outputJsonl);
        var temporary = AtomicPluginOutput.CreateTemporaryPath(outputJsonl);
        try
        {
            using (var writer = new StreamWriter(
                       temporary,
                       false,
                       new System.Text.UTF8Encoding(false)))
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

    private static System.Text.Encoding DetectEncoding(
        IEnumerable<PluginRawSubrecord> rawSubrecords)
    {
        var rows = rawSubrecords.ToArray();
        var hasUtf8NonAscii = rows.Any(raw =>
            raw.Payload.Any(static value => value >= 0x80)
            && PluginBinaryInvariant.IsStrictUtf8Payload(raw.Payload));
        var hasLegacyNonAscii = rows.Any(raw =>
            raw.Payload.Any(static value => value >= 0x80)
            && !PluginBinaryInvariant.IsStrictUtf8Payload(raw.Payload));
        if (hasUtf8NonAscii && hasLegacyNonAscii)
        {
            throw new InvalidDataException(
                "mixed UTF-8 and legacy non-ASCII plugin strings are not supported");
        }
        System.Text.Encoding.RegisterProvider(
            System.Text.CodePagesEncodingProvider.Instance);
        return hasUtf8NonAscii
            ? new System.Text.UTF8Encoding(false, true)
            : System.Text.Encoding.GetEncoding(
                1252,
                System.Text.EncoderFallback.ExceptionFallback,
                System.Text.DecoderFallback.ExceptionFallback);
    }

    private static Dictionary<FieldKey, ExportField> BuildFields(SkyrimMod mod)
    {
        var fields = new Dictionary<FieldKey, ExportField>();
        Add(fields, mod.MagicEffects, "MGEF", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.MagicEffects, "MGEF", "DNAM", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Spells, "SPEL", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Spells, "SPEL", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Armors, "ARMO", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Armors, "ARMO", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Weapons, "WEAP", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, EnumerateCells(mod), "CELL", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Classes, "CLAS", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Colors, "CLFM", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Perks, "PERK", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Perks, "PERK", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Factions, "FACT", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.ObjectEffects, "ENCH", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Containers, "CONT", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.MiscItems, "MISC", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Ingestibles, "ALCH", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Ingestibles, "ALCH", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Worldspaces, "WRLD", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.DialogTopics.Records, "DIAL", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Quests, "QUST", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        Add(fields, mod.Quests, "QUST", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Messages, "MESG", "DESC", static x => x.FormKey, static x => x.EditorID, static x => x.Description?.String ?? "");
        Add(fields, mod.Messages, "MESG", "FULL", static x => x.FormKey, static x => x.EditorID, static x => x.Name?.String ?? "");
        foreach (var message in mod.Messages)
        {
            AddRepeated(
                fields,
                "MESG",
                message.FormKey,
                message.EditorID,
                "ITXT",
                message.MenuButtons.Select(static button => button.Text?.String ?? ""));
        }
        foreach (var response in mod.DialogTopics.Records.SelectMany(static topic => topic.Responses))
        {
            Add(fields, "INFO", response.FormKey, string.Empty, "RNAM", response.Prompt?.String ?? "");
            AddRepeated(
                fields,
                "INFO",
                response.FormKey,
                string.Empty,
                "NAM1",
                response.Responses.Select(static item => item.Text?.String ?? ""));
        }
        return fields;
    }

    private static void Add<TRecord>(
        IDictionary<FieldKey, ExportField> fields,
        IEnumerable<TRecord> records,
        string recordType,
        string subrecordType,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source)
    {
        foreach (var record in records)
        {
            Add(
                fields,
                recordType,
                formKey(record),
                editorId(record) ?? string.Empty,
                subrecordType,
                source(record));
        }
    }

    private static void Add(
        IDictionary<FieldKey, ExportField> fields,
        string recordType,
        FormKey formKey,
        string editorId,
        string subrecordType,
        string source)
    {
        if (string.IsNullOrWhiteSpace(source)) return;
        var key = new FieldKey(recordType, formKey, subrecordType, 0);
        if (!fields.TryAdd(key, new(editorId, source)))
        {
            throw new InvalidDataException($"duplicate Skyrim export identity: {key}");
        }
    }

    private static void AddRepeated(
        IDictionary<FieldKey, ExportField> fields,
        string recordType,
        FormKey formKey,
        string? editorId,
        string subrecordType,
        IEnumerable<string> values)
    {
        var occurrenceIndex = 0;
        foreach (var source in values)
        {
            if (!string.IsNullOrWhiteSpace(source))
            {
                var key = new FieldKey(
                    recordType,
                    formKey,
                    subrecordType,
                    occurrenceIndex);
                if (!fields.TryAdd(key, new(editorId ?? string.Empty, source)))
                {
                    throw new InvalidDataException(
                        $"duplicate Skyrim export identity: {key}");
                }
            }
            occurrenceIndex++;
        }
    }

    private static IEnumerable<Cell> EnumerateCells(SkyrimMod mod)
    {
        foreach (var block in mod.Cells.Records)
        foreach (var subBlock in block.SubBlocks)
        foreach (var cell in subBlock.Cells)
            yield return cell;
    }

    private sealed record RawFieldKey(
        string RecordType,
        uint RawFormId,
        string SubrecordType);
    private sealed record FieldKey(
        string RecordType,
        FormKey FormKey,
        string SubrecordType,
        int OccurrenceIndex);
    private sealed record ExportField(string EditorId, string Source);
}

internal static class SkyrimPluginTraits
{
    private const byte UnsupportedLightFormIdMarker = 0xFE;

    public static PluginTraits Inspect(
        string inputPlugin,
        SkyrimMod mod,
        IEnumerable<uint>? majorRecordFormIds = null)
    {
        var flags = mod.ModHeader.Flags;
        return new(
            flags.HasFlag(SkyrimModHeader.HeaderFlag.Localized),
            string.Equals(
                Path.GetExtension(inputPlugin),
                ".esl",
                StringComparison.OrdinalIgnoreCase),
            flags.HasFlag(SkyrimModHeader.HeaderFlag.Small),
            majorRecordFormIds?.Any(static rawFormId =>
                rawFormId >> 24 == UnsupportedLightFormIdMarker));
    }
}
