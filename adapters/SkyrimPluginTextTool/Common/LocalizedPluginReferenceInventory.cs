using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda.Plugins.Records;

internal sealed record LocalizedPluginReferenceRow(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("game_id")] string GameId,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("plugin")] string Plugin,
    [property: JsonPropertyName("mod_key")] string ModKey,
    [property: JsonPropertyName("localized_flag")] bool LocalizedFlag,
    [property: JsonPropertyName("record_type")] string RecordType,
    [property: JsonPropertyName("form_id")] string FormId,
    [property: JsonPropertyName("owner_mod_key")] string OwnerModKey,
    [property: JsonPropertyName("local_id")] uint LocalId,
    [property: JsonPropertyName("master_style")] string MasterStyle,
    [property: JsonPropertyName("master_style_evidence")] string MasterStyleEvidence,
    [property: JsonPropertyName("field_path")] string FieldPath,
    [property: JsonPropertyName("subrecord_type")] string SubrecordType,
    [property: JsonPropertyName("occurrence_index")] int OccurrenceIndex,
    [property: JsonPropertyName("table_type")] string TableType,
    [property: JsonPropertyName("string_id")] uint StringId);

internal sealed record LocalizedPluginReferenceInventoryResult(
    IReadOnlyList<LocalizedPluginReferenceRow> Rows,
    PluginTraits Traits,
    string BlockedReason,
    string MasterStyleContextPath)
{
    public bool Blocked => !string.IsNullOrWhiteSpace(BlockedReason);
}

internal static class LocalizedPluginReferenceInventory
{
    public static LocalizedPluginReferenceInventoryResult Read(
        string gameId,
        string inputPlugin,
        string relativeInputPath,
        IModGetter mod,
        PluginTraits traits,
        PluginMasterStyleContext masterContext)
    {
        if (traits.Localized != true)
        {
            return new(
                [],
                traits,
                "Plugin does not have the localized header flag.",
                masterContext.ContextPath);
        }

        var resolver = new PluginFormKeyResolver(mod, masterContext);
        var occurrences = new Dictionary<(string Record, uint FormId, string Subrecord), int>();
        var rows = new List<LocalizedPluginReferenceRow>();
        foreach (var raw in PluginBinaryInvariant.ReadRawSubrecords(inputPlugin))
        {
            if (!PluginFieldContract.TryGetFieldPath(
                    gameId,
                    raw.RecordType,
                    raw.SubrecordType,
                    out var fieldPath)
                || !PluginFieldContract.TryGetStringTableType(
                    gameId,
                    raw.RecordType,
                    raw.SubrecordType,
                    out var tableType))
            {
                continue;
            }
            if (raw.Payload.Length != sizeof(uint))
            {
                throw new InvalidDataException(
                    $"Localized field {raw.RecordType}/{raw.SubrecordType} must contain "
                    + $"one 32-bit string ID, found {raw.Payload.Length} bytes.");
            }

            var occurrenceKey = (raw.RecordType, raw.RawFormId, raw.SubrecordType);
            occurrences.TryGetValue(occurrenceKey, out var occurrenceIndex);
            occurrences[occurrenceKey] = occurrenceIndex + 1;
            var stringId = BinaryPrimitives.ReadUInt32LittleEndian(raw.Payload);
            if (stringId == 0)
            {
                continue;
            }

            if (!resolver.TryResolve(
                    raw.RawFormId.ToString("X8"),
                    out _,
                    out var identity,
                    out var reason))
            {
                throw new InvalidDataException(
                    $"Localized reference could not resolve {raw.RecordType} "
                    + $"{raw.RawFormId:X8}: {reason}");
            }
            rows.Add(new(
                1,
                gameId,
                relativeInputPath,
                Path.GetFileName(inputPlugin),
                mod.ModKey.FileName.String,
                true,
                raw.RecordType,
                raw.RawFormId.ToString("X8"),
                identity.FormKey.ModKey.FileName.String,
                identity.LocalId,
                identity.MasterStyle,
                identity.EvidenceSource,
                fieldPath,
                raw.SubrecordType,
                occurrenceIndex,
                tableType,
                stringId));
        }
        return new(rows, traits, string.Empty, masterContext.ContextPath);
    }

    public static void WriteJsonl(
        string outputJsonl,
        IEnumerable<LocalizedPluginReferenceRow> rows)
    {
        AtomicPluginOutput.PrepareTarget(outputJsonl);
        var temporary = AtomicPluginOutput.CreateTemporaryPath(outputJsonl);
        var options = new JsonSerializerOptions
        {
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        };
        try
        {
            using (var writer = new StreamWriter(
                       temporary,
                       false,
                       new UTF8Encoding(false)))
            {
                foreach (var row in rows)
                {
                    writer.WriteLine(JsonSerializer.Serialize(row, options));
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
}
