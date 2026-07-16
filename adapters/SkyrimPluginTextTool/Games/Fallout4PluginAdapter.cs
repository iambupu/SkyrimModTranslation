using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;

internal static class Fallout4PluginAdapter
{
    public static AdapterResult Apply(
        string inputPlugin,
        string outputPlugin,
        List<TranslationRow> rows,
        bool dryRun)
    {
        var result = new AdapterResult { Traits = PluginTraits.FromPath(inputPlugin) };
        if (result.Traits.LightByExtension == true)
        {
            try
            {
                var lightMod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4);
                var lightMajorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
                result.Traits = Fallout4PluginTraits.Inspect(
                    inputPlugin,
                    lightMod,
                    lightMajorRecordFormIds);
            }
            catch (Exception exc)
            {
                result.Skipped.Add($"Best-effort ESL trait inspection failed: {exc.Message}");
            }
            result.Unsupported.Add(
                "experimental_limit: Fallout 4 ESL writeback is not supported until light FormID resolution is fixture-backed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }

        var mod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(inputPlugin);
        var traits = Fallout4PluginTraits.Inspect(
            inputPlugin,
            mod,
            majorRecordFormIds);
        result.Traits = traits;
        if (traits.LightByHeader == true)
        {
            result.Unsupported.Add(
                "experimental_limit: Fallout 4 light plugin writeback is not supported until light FormID resolution is fixture-backed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }
        if (traits.Localized == true)
        {
            result.Unsupported.Add("TES4 localized flag: Fallout 4 string-table writeback is not implemented.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }
        if (traits.ContainsUnsupportedLightFormIds == true)
        {
            result.Unsupported.Add(
                "experimental_limit: Fallout 4 plugin contains 0xFE/light FormIDs that cannot be resolved safely.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }

        var resolver = new PluginFormKeyResolver(mod);
        PreflightRows(inputPlugin, rows, resolver, result);
        if (result.Unsupported.Count > 0)
        {
            result.Skipped.Add("Plugin write skipped because schema or identity preflight failed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }

        foreach (var row in rows)
        {
            if (!Fallout4PluginFieldRegistry.TryApply(mod, row, result))
            {
                result.Unsupported.Add(Describe(row, $"unsupported Fallout 4 field {row.FieldPath}"));
            }
        }

        if (result.Missing.Count > 0 || result.Unsupported.Count > 0)
        {
            result.Skipped.Add("Plugin write skipped because one or more rows failed closed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
        }
        else if (dryRun)
        {
            result.Skipped.Add("Dry run: plugin write skipped.");
        }
        else
        {
            WriteValidateAndCommit(inputPlugin, mod, outputPlugin, rows, result);
        }
        return result;
    }

    private static void PreflightRows(
        string inputPlugin,
        IEnumerable<TranslationRow> rows,
        PluginFormKeyResolver resolver,
        AdapterResult result)
    {
        foreach (var row in rows)
        {
            if (!string.Equals(row.GameId, "fallout4", StringComparison.Ordinal))
            {
                result.Unsupported.Add(Describe(row, $"row game_id {row.GameId} does not match fallout4"));
                continue;
            }
            if (!string.Equals(row.Plugin, Path.GetFileName(inputPlugin), StringComparison.OrdinalIgnoreCase))
            {
                result.Unsupported.Add(Describe(row, "row plugin does not match input plugin"));
                continue;
            }
            if (!PluginFieldContract.TryValidate("fallout4", row, out var fieldReason))
            {
                result.Unsupported.Add(Describe(row, fieldReason));
                continue;
            }
            if (!resolver.TryResolve(row.FormId, out var formKey, out var formReason))
            {
                result.Unsupported.Add(Describe(row, formReason));
                continue;
            }
            row.ResolvedFormKey = formKey;
        }
    }

    private static void WriteValidateAndCommit(
        string inputPlugin,
        Fallout4Mod mod,
        string outputPlugin,
        IReadOnlyCollection<TranslationRow> rows,
        AdapterResult result)
    {
        var inputSnapshot = PluginStructureSnapshot.From(mod);
        var temporaryPlugin = AtomicPluginOutput.CreateTemporaryPath(outputPlugin);
        try
        {
            mod.BeginWrite
                .ToPath(temporaryPlugin)
                .WithLoadOrderFromHeaderMasters()
                .WithNoDataFolder()
                .NoModKeySync()
                .WithUtf8Encoding()
                .WithMastersListContent(MastersListContentOption.NoCheck)
                .Write();

            var temporaryReparse = Fallout4Mod.CreateFromBinary(temporaryPlugin, Fallout4Release.Fallout4);
            var temporarySnapshot = PluginStructureSnapshot.From(temporaryReparse);
            inputSnapshot.ApplyComparison(temporarySnapshot, result);
            result.ApplyBinaryInvariant(PluginBinaryInvariant.Verify(inputPlugin, temporaryPlugin, rows));
            if (!result.RecordCountPreserved || !result.FormKeySetPreserved || !result.MastersPreserved || !result.BinaryInvariantVerified)
            {
                result.Unsupported.Add("Temporary output failed structural validation.");
                AtomicPluginOutput.CleanupFailure(temporaryPlugin, outputPlugin);
                return;
            }

            AtomicPluginOutput.Commit(temporaryPlugin, outputPlugin);
            _ = Fallout4Mod.CreateFromBinary(outputPlugin, Fallout4Release.Fallout4);
            result.ReparseSucceeded = true;
        }
        catch (Exception ex)
        {
            result.ReparseSucceeded = false;
            result.Unsupported.Add($"Fallout 4 output write/reparse failed: {ex.Message}");
            AtomicPluginOutput.CleanupFailure(temporaryPlugin, outputPlugin);
        }
        finally
        {
            if (File.Exists(temporaryPlugin))
            {
                File.Delete(temporaryPlugin);
            }
        }
    }

    private static string Describe(TranslationRow row, string action) =>
        $"{row.RecordType} {row.FormId} {row.FieldPath} {row.EditorId}: {action}";

}
