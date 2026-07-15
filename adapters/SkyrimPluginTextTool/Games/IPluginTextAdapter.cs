internal sealed record PluginTextRequest(
    string GameId,
    string CapabilityLevel,
    string InputPlugin,
    string OutputPlugin,
    bool DryRun);

internal sealed record PluginExportRequest(
    string GameId,
    string CapabilityLevel,
    string InputPlugin,
    string RelativeInputPath,
    string OutputJsonl);

internal sealed record PluginExportResult(int RowCount, string Coverage);

internal interface IPluginTextAdapter
{
    string GameId { get; }
    string MutagenRelease { get; }
    AdapterResult Apply(PluginTextRequest request, List<TranslationRow> rows);
    AdapterResult Verify(PluginTextRequest request, List<TranslationRow> rows);
    PluginExportResult Export(PluginExportRequest request);
}
