using Mutagen.Bethesda.Fallout4;

internal sealed class Fallout4PluginTextAdapter : IPluginTextAdapter
{
    public string GameId => "fallout4";
    public string MutagenRelease => "Fallout4";

    public AdapterResult Apply(PluginTextRequest request, List<TranslationRow> rows)
    {
        var identityFailure = ValidateIdentity(request, rows);
        if (identityFailure is not null)
        {
            AtomicPluginOutput.CleanupFailure(string.Empty, request.OutputPlugin);
            return identityFailure;
        }

        var result = Fallout4PluginAdapter.Apply(
            request.InputPlugin,
            request.OutputPlugin,
            rows,
            request.DryRun);
        result.ReparseTarget = "final-output";
        return result;
    }

    public AdapterResult Verify(PluginTextRequest request, List<TranslationRow> rows)
    {
        if (!File.Exists(request.OutputPlugin))
        {
            throw new FileNotFoundException(
                "Output plugin does not exist for verification.",
                request.OutputPlugin);
        }
        var result = ValidateIdentity(request, rows)
            ?? new AdapterResult { ReparseTarget = "final-output" };
        result.ReparseTarget = "final-output";
        result.Traits = PluginTraits.FromPath(request.InputPlugin);
        foreach (var row in rows)
        {
            if (!PluginFieldContract.TryValidate(request.GameId, row, out var reason))
            {
                result.Unsupported.Add(Describe(row, reason));
            }
        }

        try
        {
            var input = Fallout4Mod.CreateFromBinary(
                request.InputPlugin,
                Fallout4Release.Fallout4);
            var inputMajorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(request.InputPlugin);
            var traits = Fallout4PluginTraits.Inspect(
                request.InputPlugin,
                input,
                inputMajorRecordFormIds);
            result.Traits = traits;
            var output = Fallout4Mod.CreateFromBinary(
                request.OutputPlugin,
                Fallout4Release.Fallout4);
            PluginStructureSnapshot.From(input).ApplyComparison(
                PluginStructureSnapshot.From(output),
                result);
            result.ReparseSucceeded = true;
            result.ApplyBinaryInvariant(
                PluginBinaryInvariant.Verify(
                    request.InputPlugin,
                    request.OutputPlugin,
                    rows));
        }
        catch (Exception exc)
        {
            result.ReparseSucceeded = false;
            result.Unsupported.Add($"Output reparse failed: {exc.Message}");
        }
        return result;
    }

    public PluginExportResult Export(PluginExportRequest request)
    {
        var export = Fallout4PluginExporter.Export(
            request.InputPlugin,
            request.RelativeInputPath);
        if (!export.Blocked)
        {
            Fallout4PluginExporter.WriteJsonl(request.OutputJsonl, export.Rows);
        }
        return new PluginExportResult(
            export.Rows.Count,
            "Fallout 4 non-localized fields supported by the controlled writeback adapter",
            export.Traits,
            export.Blocked,
            export.BlockedReason);
    }

    private static AdapterResult? ValidateIdentity(
        PluginTextRequest request,
        IEnumerable<TranslationRow> rows)
    {
        var result = new AdapterResult
        {
            ReparseTarget = "final-output",
            Traits = PluginTraits.FromPath(request.InputPlugin),
        };
        foreach (var row in rows)
        {
            if (row.SchemaVersion >= 2
                && !string.Equals(row.GameId, request.GameId, StringComparison.Ordinal))
            {
                result.Unsupported.Add(
                    Describe(
                        row,
                        $"row game_id {row.GameId} does not match {request.GameId}"));
            }
            if (row.SchemaVersion >= 2
                && !string.Equals(
                    row.Plugin,
                    Path.GetFileName(request.InputPlugin),
                    StringComparison.OrdinalIgnoreCase))
            {
                result.Unsupported.Add(Describe(row, "row plugin does not match input plugin"));
            }
        }
        return result.Unsupported.Count == 0 ? null : result;
    }

    private static string Describe(TranslationRow row, string action) =>
        $"{row.RecordType} {row.FormId} {row.FieldPath} {row.EditorId}: {action}";
}
