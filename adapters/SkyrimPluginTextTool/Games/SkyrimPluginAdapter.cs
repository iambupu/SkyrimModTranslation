using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Skyrim;

internal sealed class SkyrimPluginAdapter : IPluginTextAdapter
{
    public string GameId => "skyrim-se";
    public string MutagenRelease => "SkyrimSE";

    public AdapterResult Apply(PluginTextRequest request, List<TranslationRow> candidateRows)
    {
        var mod = SkyrimMod.CreateFromBinary(request.InputPlugin, SkyrimRelease.SkyrimSE);
        var resolver = new PluginFormKeyResolver(mod);
        var buttonCursor = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        var dialogResponseIndexes = BuildDialogResponseIndexes(candidateRows);
        var result = new AdapterResult { ReparseTarget = "temporary-output" };
        var applied = result.Applied;
        var skipped = result.Skipped;
        var missing = result.Missing;
        var unsupported = result.Unsupported;

        foreach (var row in candidateRows)
        {
            if (row.SchemaVersion >= 2
                && !string.Equals(row.GameId, request.GameId, StringComparison.Ordinal))
            {
                unsupported.Add(
                    Describe(row, $"row game_id {row.GameId} does not match {request.GameId}"));
                continue;
            }
            if (row.SchemaVersion >= 2
                && !string.Equals(
                    row.Plugin,
                    Path.GetFileName(request.InputPlugin),
                    StringComparison.OrdinalIgnoreCase))
            {
                unsupported.Add(Describe(row, "row plugin does not match input plugin"));
                continue;
            }
            if (!PluginFieldContract.TryValidate(request.GameId, row, out var fieldReason))
            {
                unsupported.Add(Describe(row, fieldReason));
                continue;
            }
            if (row.SchemaVersion >= 2)
            {
                if (!resolver.TryResolve(row.FormId, out var formKey, out var formReason))
                {
                    unsupported.Add(Describe(row, formReason));
                    continue;
                }
                row.ResolvedFormKey = formKey;
            }
        }

        if (unsupported.Count == 0)
        {
            foreach (var row in candidateRows)
            {
                switch (row.RecordType)
                {
                case "MGEF":
                    ApplyMagicEffect(mod, row, applied, missing, unsupported);
                    break;
                case "SPEL":
                    ApplySpell(mod, row, applied, missing, unsupported);
                    break;
                case "ARMO":
                    ApplyArmor(mod, row, applied, missing, unsupported);
                    break;
                case "WEAP":
                    ApplyWeapon(mod, row, applied, missing, unsupported);
                    break;
                case "CELL":
                    ApplyCell(mod, row, applied, missing, unsupported);
                    break;
                case "CLAS":
                    ApplyClass(mod, row, applied, missing, unsupported);
                    break;
                case "CLFM":
                    ApplyColorRecord(mod, row, applied, missing, unsupported);
                    break;
                case "PERK":
                    ApplyPerk(mod, row, applied, missing, unsupported);
                    break;
                case "FACT":
                    ApplyNamedRecord(mod.Factions, "Faction", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "ENCH":
                    ApplyNamedRecord(mod.ObjectEffects, "ObjectEffect", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "CONT":
                    ApplyNamedRecord(mod.Containers, "Container", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "MISC":
                    ApplyNamedRecord(mod.MiscItems, "MiscItem", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "ALCH":
                    ApplyIngestible(mod, row, applied, missing, unsupported);
                    break;
                case "WRLD":
                    ApplyNamedRecord(mod.Worldspaces, "Worldspace", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "DIAL":
                    ApplyNamedRecord(mod.DialogTopics, "DialogTopic", row, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "INFO":
                    ApplyDialogResponses(mod, row, dialogResponseIndexes, applied, missing, unsupported);
                    break;
                case "QUST":
                    ApplyQuest(mod, row, applied, missing, unsupported);
                    break;
                case "MESG":
                    ApplyMessage(mod, row, buttonCursor, applied, missing, unsupported);
                    break;
                default:
                    unsupported.Add(Describe(row, $"unsupported record type {row.RecordType}"));
                    break;
                }
            }
        }

        if (missing.Count > 0 || unsupported.Count > 0)
        {
            skipped.Add("Plugin write skipped because one or more rows failed closed.");
            AtomicPluginOutput.CleanupFailure(string.Empty, request.OutputPlugin);
        }
        else if (!request.DryRun)
        {
            WriteValidateAndCommit(
                request.InputPlugin,
                mod,
                request.OutputPlugin,
                candidateRows,
                result);
        }
        else
        {
            skipped.Add("Dry run: plugin write skipped.");
        }

        return result;
    }

    public AdapterResult Verify(PluginTextRequest request, List<TranslationRow> candidateRows)
    {
        if (!File.Exists(request.OutputPlugin))
        {
            throw new FileNotFoundException(
                "Output plugin does not exist for verification.",
                request.OutputPlugin);
        }
        var result = new AdapterResult { ReparseTarget = "final-output" };
        foreach (var row in candidateRows)
        {
            if (row.SchemaVersion >= 2
                && !string.Equals(row.GameId, request.GameId, StringComparison.Ordinal))
            {
                result.Unsupported.Add(
                    Describe(row, $"row game_id {row.GameId} does not match {request.GameId}"));
            }
            if (row.SchemaVersion >= 2
                && !string.Equals(
                    row.Plugin,
                    Path.GetFileName(request.InputPlugin),
                    StringComparison.OrdinalIgnoreCase))
            {
                result.Unsupported.Add(Describe(row, "row plugin does not match input plugin"));
            }
            if (!PluginFieldContract.TryValidate(request.GameId, row, out var reason))
            {
                result.Unsupported.Add(Describe(row, reason));
            }
        }

        try
        {
            var input = SkyrimMod.CreateFromBinary(request.InputPlugin, SkyrimRelease.SkyrimSE);
            var output = SkyrimMod.CreateFromBinary(request.OutputPlugin, SkyrimRelease.SkyrimSE);
            PluginStructureSnapshot.From(input).ApplyComparison(
                PluginStructureSnapshot.From(output),
                result);
            result.ReparseSucceeded = true;
            result.ApplyBinaryInvariant(
                PluginBinaryInvariant.Verify(
                    request.InputPlugin,
                    request.OutputPlugin,
                    candidateRows));
        }
        catch (Exception exc)
        {
            result.ReparseSucceeded = false;
            result.Unsupported.Add($"Output reparse failed: {exc.Message}");
        }

        return result;
    }

    public PluginExportResult Export(PluginExportRequest request) =>
        throw new NotSupportedException(
            $"Mutagen release SkyrimSE does not use the controlled C# export path for {request.GameId}.");

    private static void ApplyMagicEffect(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.MagicEffects.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "MagicEffect not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DNAM")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported MagicEffect subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplySpell(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Spells.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Spell not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Spell subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyArmor(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Armors.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Armor not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Armor subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyWeapon(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Weapons.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Weapon not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Weapon subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyCell(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = EnumerateCells(mod).FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Cell not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Cell subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyColorRecord(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Colors.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "ColorRecord not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported ColorRecord subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyClass(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Classes.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Class not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Class subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyPerk(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Perks.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Perk not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Perk subrecord {row.SubrecordType}"));
        }
    }

    private static IEnumerable<Cell> EnumerateCells(SkyrimMod mod)
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

    private static void ApplyNamedRecord<TRecord>(
        IEnumerable<TRecord> records,
        string recordLabel,
        TranslationRow row,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source,
        Action<TRecord, string> setName,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
        where TRecord : class
    {
        var record = records.FirstOrDefault(item => MatchesRecord(row, formKey(item), editorId(item)));
        if (record is null)
        {
            missing.Add(Describe(row, $"{recordLabel} not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, source(record), missing)) return;
            setName(record, row.Target);
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported {recordLabel} subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyIngestible(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Ingestibles.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Ingestible not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Ingestible subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyQuest(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Quests.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Quest not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Quest subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyMessage(
        SkyrimMod mod,
        TranslationRow row,
        Dictionary<string, int> buttonCursor,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
    {
        var record = mod.Messages.FirstOrDefault(item => MatchesRecord(row, item.FormKey, item.EditorID));
        if (record is null)
        {
            missing.Add(Describe(row, "Message not found"));
            return;
        }

        if (row.SubrecordType == "DESC")
        {
            if (!SourceMatches(row, record.Description?.String ?? "", missing)) return;
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else if (row.SubrecordType == "FULL")
        {
            if (!SourceMatches(row, record.Name?.String ?? "", missing)) return;
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "ITXT")
        {
            var key = $"{row.FormId}|{row.EditorId}";
            buttonCursor.TryGetValue(key, out var buttonIndex);
            if (buttonIndex >= record.MenuButtons.Count)
            {
                missing.Add(Describe(row, $"Message button index {buttonIndex} not found"));
                return;
            }

            if (!SourceMatches(row, record.MenuButtons[buttonIndex].Text?.String ?? "", missing)) return;
            record.MenuButtons[buttonIndex].Text = row.Target;
            buttonCursor[key] = buttonIndex + 1;
            applied.Add(Describe(row, $"MenuButtons[{buttonIndex}].Text"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Message subrecord {row.SubrecordType}"));
        }
    }

    private static Dictionary<string, int> BuildDialogResponseIndexes(List<TranslationRow> candidateRows)
    {
        var indexes = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        var groups = candidateRows
            .Where(static row => row.RecordType == "INFO" && row.SubrecordType == "NAM1")
            .GroupBy(static row => CanonicalRawFormId(row.FormId));

        foreach (var group in groups)
        {
            var responseIndex = 0;
            foreach (var row in group.OrderBy(static row => row.SubrecordIndex))
            {
                indexes[DialogResponseIndexKey(row)] = responseIndex;
                responseIndex++;
            }
        }

        return indexes;
    }

    private static string DialogResponseIndexKey(TranslationRow row)
    {
        return $"{CanonicalRawFormId(row.FormId)}|{row.SubrecordIndex}";
    }

    private static void ApplyDialogResponses(
        SkyrimMod mod,
        TranslationRow row,
        Dictionary<string, int> dialogResponseIndexes,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
    {
        var responseRecord = mod.DialogTopics.Records
            .SelectMany(static topic => topic.Responses)
            .FirstOrDefault(item => MatchesDialogRecord(row, item.FormKey));
        if (responseRecord is null)
        {
            missing.Add(Describe(row, "DialogResponses not found"));
            return;
        }

        if (row.SubrecordType == "RNAM")
        {
            if (!SourceMatches(row, responseRecord.Prompt?.String ?? "", missing)) return;
            responseRecord.Prompt = row.Target;
            applied.Add(Describe(row, "Prompt"));
            return;
        }

        if (row.SubrecordType != "NAM1")
        {
            unsupported.Add(Describe(row, $"unsupported DialogResponses subrecord {row.SubrecordType}"));
            return;
        }

        if (responseRecord.Responses.Count == 0)
        {
            missing.Add(Describe(row, "DialogResponses has no response text entries"));
            return;
        }

        if (!dialogResponseIndexes.TryGetValue(DialogResponseIndexKey(row), out var responseIndex))
        {
            missing.Add(Describe(row, "Dialog response index not found"));
            return;
        }

        if (responseIndex >= responseRecord.Responses.Count)
        {
            missing.Add(Describe(row, $"Dialog response index {responseIndex} not found"));
            return;
        }

        if (!SourceMatches(row, responseRecord.Responses[responseIndex].Text?.String ?? "", missing)) return;
        responseRecord.Responses[responseIndex].Text = row.Target;
        applied.Add(Describe(row, $"Responses[{responseIndex}].Text"));
    }

    private static void WriteValidateAndCommit(
        string inputPlugin,
        SkyrimMod mod,
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

            var temporaryReparse = SkyrimMod.CreateFromBinary(temporaryPlugin, SkyrimRelease.SkyrimSE);
            inputSnapshot.ApplyComparison(PluginStructureSnapshot.From(temporaryReparse), result);
            result.ApplyBinaryInvariant(PluginBinaryInvariant.Verify(inputPlugin, temporaryPlugin, rows));
            if (!result.RecordCountPreserved || !result.FormKeySetPreserved || !result.MastersPreserved || !result.BinaryInvariantVerified)
            {
                result.Unsupported.Add("Temporary output failed structural validation.");
                AtomicPluginOutput.CleanupFailure(temporaryPlugin, outputPlugin);
                return;
            }

            result.ReparseSucceeded = true;
            AtomicPluginOutput.Commit(temporaryPlugin, outputPlugin);
        }
        catch (Exception ex)
        {
            result.ReparseSucceeded = false;
            result.Unsupported.Add($"Skyrim output write/reparse failed: {ex.Message}");
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

    private static bool SameEditorId(string? left, string? right)
    {
        return string.Equals(left ?? string.Empty, right ?? string.Empty, StringComparison.OrdinalIgnoreCase);
    }

    private static bool MatchesRecord(TranslationRow row, FormKey formKey, string? editorId)
    {
        if (row.SchemaVersion < 2)
        {
            return SameEditorId(editorId, row.EditorId);
        }
        if (row.ResolvedFormKey is not FormKey expected || expected != formKey)
        {
            return false;
        }
        return string.IsNullOrWhiteSpace(row.EditorId) || SameEditorId(editorId, row.EditorId);
    }

    private static bool SourceMatches(TranslationRow row, string current, List<string> missing)
    {
        if (row.SchemaVersion < 2 || string.Equals(current, row.Source, StringComparison.Ordinal))
        {
            return true;
        }
        missing.Add(Describe(row, "source text does not match current record value"));
        return false;
    }

    private static bool MatchesDialogRecord(TranslationRow row, FormKey formKey)
    {
        if (row.SchemaVersion >= 2)
        {
            return row.ResolvedFormKey is FormKey expected && expected == formKey;
        }
        return string.Equals(
            CanonicalRawFormId(row.FormId)[^6..],
            formKey.IDString(),
            StringComparison.OrdinalIgnoreCase);
    }

    private static string CanonicalRawFormId(string? value)
    {
        var trimmed = (value ?? string.Empty).Trim();
        if (trimmed.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
        {
            trimmed = trimmed[2..];
        }
        trimmed = trimmed.ToUpperInvariant();
        return trimmed.PadLeft(8, '0');
    }

    private static string Describe(TranslationRow row, string action)
    {
        return $"{row.RecordType} {row.FormId} {row.SubrecordType} {row.EditorId}: {action}";
    }

}
