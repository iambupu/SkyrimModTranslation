using System.Buffers.Binary;
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
    [property: JsonPropertyName("payload_size")] int PayloadSize,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("target")] string Target,
    [property: JsonPropertyName("risk")] string Risk,
    [property: JsonPropertyName("writeback")] string Writeback,
    [property: JsonPropertyName("reason")] string Reason);

internal sealed record Fallout4PluginExportResult(
    IReadOnlyList<Fallout4PluginExportRow> Rows,
    PluginTraits Traits,
    string BlockedReason,
    string MasterStyleContextPath,
    bool? ReferencesLightMaster,
    bool? TargetsLightOwner)
{
    public bool Blocked => !string.IsNullOrWhiteSpace(BlockedReason);
}

internal static class Fallout4PluginExporter
{
    public static LocalizedPluginReferenceInventoryResult InventoryLocalizedReferences(
        string projectRoot,
        string inputPlugin,
        string relativeInputPath,
        string? masterStyleManifest,
        bool requireCompleteMasterStyleMap = false)
    {
        var targetRawFormIds = string.IsNullOrWhiteSpace(masterStyleManifest)
            && !requireCompleteMasterStyleMap
            ? new HashSet<uint>()
            : PluginBinaryInvariant.ReadRawSubrecords(inputPlugin)
            .Where(raw => PluginFieldContract.TryGetFieldPath(
                "fallout4",
                raw.RecordType,
                raw.SubrecordType,
                out _)
                && PluginFieldContract.TryGetStringTableType(
                    "fallout4",
                    raw.RecordType,
                    raw.SubrecordType,
                    out _)
                && raw.Payload.Length == sizeof(uint)
                && BinaryPrimitives.ReadUInt32LittleEndian(raw.Payload) != 0)
            .Select(static raw => raw.RawFormId)
            .ToHashSet();
        var masterContext = PluginMasterStyleContext.Resolve(
            projectRoot,
            inputPlugin,
            "fallout4",
            masterStyleManifest,
            requireCompleteMap: requireCompleteMasterStyleMap,
            targetRawFormIds: targetRawFormIds,
            manifestDefinesTargetScope: !requireCompleteMasterStyleMap);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
        var readParameters = new BinaryReadParameters
        {
            MasterFlagsLookup = masterContext.MasterFlagsLookup,
        };
        var mod = Fallout4Mod.CreateFromBinary(
            inputPlugin,
            Fallout4Release.Fallout4,
            readParameters);
        var traits = Fallout4PluginTraits.Inspect(
            inputPlugin,
            mod,
            majorRecordFormIds);
        if (masterContext.Required)
        {
            traits = traits with { ContainsUnsupportedLightFormIds = false };
        }
        return LocalizedPluginReferenceInventory.Read(
            "fallout4",
            inputPlugin,
            relativeInputPath,
            mod,
            traits,
            masterContext);
    }

    public static Fallout4PluginExportResult Export(
        string projectRoot,
        string inputPlugin,
        string relativeInputPath,
        string? masterStyleManifest)
    {
        var rawSubrecords = PluginBinaryInvariant.ReadRawSubrecords(inputPlugin);
        var supportedRaw = rawSubrecords
            .Where(raw => PluginFieldContract.TryGetFieldPath(
                "fallout4",
                raw.RecordType,
                raw.SubrecordType,
                out _))
            .ToArray();
        var targetRawFormIds = string.IsNullOrWhiteSpace(masterStyleManifest)
            ? new HashSet<uint>()
            : supportedRaw
                .Where(static raw => !string.IsNullOrWhiteSpace(
                    PluginBinaryInvariant.DecodeSourcePayload(raw.Payload)))
                .Select(static raw => raw.RawFormId)
                .ToHashSet();
        var masterContext = PluginMasterStyleContext.Resolve(
            projectRoot,
            inputPlugin,
            "fallout4",
            masterStyleManifest,
            targetRawFormIds: targetRawFormIds,
            manifestDefinesTargetScope: true);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
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
            MasterFlagsLookup = masterContext.MasterFlagsLookup,
            StringsParam = new StringsReadParameters
            {
                NonLocalizedEncodingOverride = mutagenEncoding,
                NonTranslatedEncodingOverride = mutagenEncoding,
            },
        };
        var mod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4, readParameters);
        var traits = Fallout4PluginTraits.Inspect(
            inputPlugin,
            mod,
            majorRecordFormIds);
        if (masterContext.Required)
        {
            traits = traits with { ContainsUnsupportedLightFormIds = false };
        }
        if (traits.Localized == true)
        {
            return new(
                [],
                traits,
                "Fallout 4 localized plugin must use the localized_delivery composite adapter; generic plugin export is blocked.",
                masterContext.ContextPath,
                masterContext.ReferencesLightMaster,
                false);
        }
        var resolver = new PluginFormKeyResolver(mod, masterContext);
        var fields = Fallout4PluginFieldRegistry.BuildExportFields(mod);
        var rows = new List<Fallout4PluginExportRow>();
        foreach (var raw in rawSubrecords)
        {
            if (!PluginFieldContract.TryGetFieldPath(
                    "fallout4",
                    raw.RecordType,
                    raw.SubrecordType,
                    out var fieldPath))
            {
                continue;
            }
            var rawSource = PluginBinaryInvariant.DecodeSourcePayload(raw.Payload);
            if (string.IsNullOrWhiteSpace(rawSource)) continue;
            if (!resolver.TryResolve(
                    raw.RawFormId.ToString("X8"),
                    out var formKey,
                    out var canonicalIdentity,
                    out var reason))
            {
                throw new InvalidDataException(
                    $"Mutagen export could not resolve {raw.RecordType} {raw.RawFormId:X8}: {reason}");
            }
            var key = new Fallout4PluginFieldKey(raw.RecordType, formKey, raw.SubrecordType);
            if (!fields.TryGetValue(key, out var field))
            {
                throw new InvalidDataException(
                    $"Mutagen export could not bind {raw.RecordType} {raw.RawFormId:X8} {raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            if (!string.Equals(rawSource, field.Source, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    $"Mutagen/raw source mismatch for {raw.RecordType} {raw.RawFormId:X8} {raw.SubrecordType}[{raw.SubrecordIndex}].");
            }
            if (!string.Equals(field.FieldPath, fieldPath, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    $"Fallout 4 exporter field path drift for {raw.RecordType}/{raw.SubrecordType}: "
                    + $"contract={fieldPath}, exporter={field.FieldPath}.");
            }
            rows.Add(new(
                2,
                "fallout4",
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
        return new(
            rows,
            traits,
            string.Empty,
            masterContext.ContextPath,
            masterContext.ReferencesLightMaster,
            TargetLightState(rows));
    }

    private static bool? TargetLightState(IEnumerable<Fallout4PluginExportRow> rows)
    {
        var styles = rows
            .Select(static row => row.MasterStyle)
            .Where(static style => !string.IsNullOrWhiteSpace(style))
            .ToArray();
        if (styles.Any(static style => string.Equals(
                style,
                "light",
                StringComparison.OrdinalIgnoreCase)))
        {
            return true;
        }
        return styles.Any(static style => string.Equals(
            style,
            "unknown",
            StringComparison.OrdinalIgnoreCase))
            ? null
            : false;
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

}
