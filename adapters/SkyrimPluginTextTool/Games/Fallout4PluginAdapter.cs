using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;

internal static class Fallout4PluginAdapter
{
    private const uint LocalizedFlag = 0x00000080;

    public static AdapterResult Apply(
        string inputPlugin,
        string outputPlugin,
        List<TranslationRow> rows,
        bool dryRun)
    {
        var result = new AdapterResult();
        if (IsLocalized(inputPlugin))
        {
            result.Unsupported.Add("TES4 localized flag: Fallout 4 string-table writeback is not implemented.");
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            return result;
        }

        var mod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4);
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
            switch ((row.RecordType, row.FieldPath))
            {
                case ("WEAP", "Name"):
                    ApplyField(mod.Weapons, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("ARMO", "Name"):
                    ApplyField(mod.Armors, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("MISC", "Name"):
                    ApplyField(mod.MiscItems, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("ALCH", "Name"):
                    ApplyField(mod.Ingestibles, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("CELL", "Name"):
                    ApplyField(EnumerateCells(mod), row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("WRLD", "Name"):
                    ApplyField(mod.Worldspaces, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("PERK", "Name"):
                    ApplyField(mod.Perks, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("PERK", "Description"):
                    ApplyField(mod.Perks, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("MGEF", "Name"):
                    ApplyField(mod.MagicEffects, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("MGEF", "Description"):
                    ApplyField(mod.MagicEffects, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("SPEL", "Name"):
                    ApplyField(mod.Spells, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("SPEL", "Description"):
                    ApplyField(mod.Spells, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("MESG", "Description"):
                    ApplyField(mod.Messages, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("QUST", "Name"):
                    ApplyField(mod.Quests, row, static item => item.FormKey, static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                default:
                    result.Unsupported.Add(Describe(row, $"unsupported Fallout 4 field {row.FieldPath}"));
                    break;
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

    private static void ApplyField<TRecord>(
        IEnumerable<TRecord> records,
        TranslationRow row,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source,
        Action<TRecord, string> assign,
        AdapterResult result)
        where TRecord : class
    {
        var record = records.FirstOrDefault(item => MatchesIdentity(row, formKey(item), editorId(item)));
        if (record is null)
        {
            result.Missing.Add(Describe(row, "record identity not found"));
            return;
        }
        if (!string.Equals(source(record), row.Source, StringComparison.Ordinal))
        {
            result.Missing.Add(Describe(row, "source text does not match current record value"));
            return;
        }
        assign(record, row.Target);
        result.Applied.Add(Describe(row, row.FieldPath));
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

    private static IEnumerable<Cell> EnumerateCells(Fallout4Mod mod)
    {
        foreach (var block in mod.Cells.Records)
        {
            foreach (var subBlock in block.SubBlocks)
            {
                foreach (var cell in subBlock.Cells)
                {
                    yield return cell;
                }
            }
        }
    }

    private static bool MatchesIdentity(TranslationRow row, FormKey formKey, string? editorId)
    {
        return row.ResolvedFormKey is FormKey expected
            && expected == formKey
            && (string.IsNullOrWhiteSpace(row.EditorId)
                || string.Equals(row.EditorId, editorId ?? string.Empty, StringComparison.OrdinalIgnoreCase));
    }

    private static string Describe(TranslationRow row, string action) =>
        $"{row.RecordType} {row.FormId} {row.FieldPath} {row.EditorId}: {action}";

    internal static bool IsLocalized(string inputPlugin)
    {
        using var stream = File.OpenRead(inputPlugin);
        Span<byte> header = stackalloc byte[12];
        if (stream.Read(header) != header.Length || !header[..4].SequenceEqual("TES4"u8))
        {
            throw new InvalidDataException("not a supported TES4-family plugin");
        }
        return (BitConverter.ToUInt32(header[8..12]) & LocalizedFlag) != 0;
    }
}
