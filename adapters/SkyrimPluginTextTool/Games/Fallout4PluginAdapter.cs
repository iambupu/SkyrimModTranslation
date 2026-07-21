using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;

internal static class Fallout4PluginAdapter
{
    public static AdapterResult Apply(
        PluginTextRequest request,
        List<TranslationRow> rows)
    {
        var masterContext = PluginMasterStyleContext.Resolve(
            request.ProjectRoot,
            request.InputPlugin,
            request.GameId,
            request.MasterStyleManifest,
            requireCompleteMap: true);
        var readParameters = new BinaryReadParameters
        {
            MasterFlagsLookup = masterContext.MasterFlagsLookup,
        };
        var mod = Fallout4Mod.CreateFromBinary(
            request.InputPlugin,
            Fallout4Release.Fallout4,
            readParameters);
        var majorRecordFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(request.InputPlugin);
        var traits = Fallout4PluginTraits.Inspect(
            request.InputPlugin,
            mod,
            majorRecordFormIds);
        var result = new AdapterResult
        {
            MasterStyleContextPath = masterContext.ContextPath,
            Traits = masterContext.Required
                ? traits with { ContainsUnsupportedLightFormIds = false }
                : traits,
        };
        if (traits.Localized == true)
        {
            result.Unsupported.Add("TES4 localized flag: Fallout 4 string-table writeback is not implemented.");
            AtomicPluginOutput.CleanupFailure(string.Empty, request.OutputPlugin);
            return result;
        }

        var resolver = new PluginFormKeyResolver(mod, masterContext);
        PreflightRows(request.InputPlugin, rows, resolver, result);
        if (result.Unsupported.Count > 0)
        {
            result.Skipped.Add("Plugin write skipped because schema or identity preflight failed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, request.OutputPlugin);
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
            AtomicPluginOutput.CleanupFailure(string.Empty, request.OutputPlugin);
        }
        else if (request.DryRun)
        {
            result.Skipped.Add("Dry run: plugin write skipped.");
        }
        else
        {
            WriteValidateAndCommit(
                request.InputPlugin,
                mod,
                request.OutputPlugin,
                rows,
                result,
                masterContext);
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
            if (!resolver.TryBindRow(row, out var formKey, out var formReason))
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
        AdapterResult result,
        PluginMasterStyleContext masterContext)
    {
        var inputSnapshot = PluginStructureSnapshot.From(mod);
        var inputLightSnapshot = PluginLightContextSnapshot.From(
            inputPlugin,
            masterContext);
        var temporaryPlugin = AtomicPluginOutput.CreateTemporaryPath(outputPlugin);
        try
        {
            mod.BeginWrite
                .ToPath(temporaryPlugin)
                .WithLoadOrderFromHeaderMasters()
                .WithKnownMasters(masterContext.KnownMasters)
                .NoModKeySync()
                .NoNextFormIDProcessing()
                .WithUtf8Encoding()
                .WithMastersListContent(MastersListContentOption.NoCheck)
                .Write();

            PluginHeaderPayloadPreserver.RestoreTes4Hedr(inputPlugin, temporaryPlugin);

            var readParameters = new BinaryReadParameters
            {
                MasterFlagsLookup = masterContext.MasterFlagsLookup,
            };
            var temporaryReparse = Fallout4Mod.CreateFromBinary(
                temporaryPlugin,
                Fallout4Release.Fallout4,
                readParameters);
            var temporarySnapshot = PluginStructureSnapshot.From(temporaryReparse);
            inputSnapshot.ApplyComparison(temporarySnapshot, result);
            inputLightSnapshot.ApplyComparison(
                PluginLightContextSnapshot.From(temporaryPlugin, masterContext),
                result);
            result.ApplyBinaryInvariant(PluginBinaryInvariant.Verify(inputPlugin, temporaryPlugin, rows));
            if (!result.RecordCountPreserved
                || !result.FormKeySetPreserved
                || !result.MastersPreserved
                || !result.CurrentMasterStylePreserved
                || !result.MasterStylesPreserved
                || !result.SmallFlagPreserved
                || !result.BinaryInvariantVerified)
            {
                result.Unsupported.Add("Temporary output failed structural validation.");
                AtomicPluginOutput.CleanupFailure(temporaryPlugin, outputPlugin);
                return;
            }

            AtomicPluginOutput.Commit(temporaryPlugin, outputPlugin);
            _ = Fallout4Mod.CreateFromBinary(
                outputPlugin,
                Fallout4Release.Fallout4,
                readParameters);
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
