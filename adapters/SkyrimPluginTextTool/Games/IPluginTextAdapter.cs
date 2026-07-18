internal sealed record PluginTextRequest(
    string GameId,
    string CapabilityLevel,
    string ProjectRoot,
    string InputPlugin,
    string OutputPlugin,
    string? MasterStyleManifest,
    bool DryRun);

internal sealed record PluginExportRequest(
    string GameId,
    string CapabilityLevel,
    string ProjectRoot,
    string InputPlugin,
    string RelativeInputPath,
    string OutputJsonl,
    string? MasterStyleManifest);

internal sealed record PluginTraits(
    bool? Localized,
    bool? LightByExtension,
    bool? LightByHeader,
    bool? ContainsUnsupportedLightFormIds)
{
    public static PluginTraits Unknown { get; } = new(null, null, null, null);

    public static PluginTraits FromPath(string inputPlugin) => new(
        null,
        string.Equals(Path.GetExtension(inputPlugin), ".esl", StringComparison.OrdinalIgnoreCase),
        null,
        null);
}

internal sealed record PluginExportResult(
    int RowCount,
    string Coverage,
    PluginTraits Traits,
    bool Blocked,
    string Reason,
    string MasterStyleContextPath = "");

internal interface IPluginTextAdapter
{
    string GameId { get; }
    string MutagenRelease { get; }
    AdapterResult Apply(PluginTextRequest request, List<TranslationRow> rows);
    AdapterResult Verify(PluginTextRequest request, List<TranslationRow> rows);
    PluginExportResult Export(PluginExportRequest request);
    LocalizedPluginReferenceInventoryResult InventoryLocalizedReferences(
        PluginExportRequest request);
}
