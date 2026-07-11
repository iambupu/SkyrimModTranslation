using Mutagen.Bethesda.Fallout4;
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
            return result;
        }

        var mod = Fallout4Mod.CreateFromBinary(inputPlugin, Fallout4Release.Fallout4);
        foreach (var row in rows)
        {
            if (row.SchemaVersion >= 2 && !string.Equals(row.GameId, "fallout4", StringComparison.OrdinalIgnoreCase))
            {
                result.Unsupported.Add(Describe(row, $"row game_id {row.GameId} does not match fallout4"));
                continue;
            }
            if (row.SchemaVersion >= 2 && !string.Equals(row.Writeback, "supported", StringComparison.OrdinalIgnoreCase))
            {
                result.Unsupported.Add(Describe(row, "field is not in the Fallout 4 writeback whitelist"));
                continue;
            }

            switch ((row.RecordType, EffectiveField(row)))
            {
                case ("WEAP", "Name"):
                    ApplyName(mod.Weapons, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("ARMO", "Name"):
                    ApplyName(mod.Armors, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("MISC", "Name"):
                    ApplyName(mod.MiscItems, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("ALCH", "Name"):
                    ApplyName(mod.Ingestibles, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("CELL", "Name"):
                    ApplyName(EnumerateCells(mod), row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("WRLD", "Name"):
                    ApplyName(mod.Worldspaces, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("PERK", "Name"):
                    ApplyName(mod.Perks, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("PERK", "Description"):
                    ApplyName(mod.Perks, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("MGEF", "Name"):
                    ApplyName(mod.MagicEffects, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("MGEF", "Description"):
                    ApplyName(mod.MagicEffects, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("SPEL", "Name"):
                    ApplyName(mod.Spells, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                case ("SPEL", "Description"):
                    ApplyName(mod.Spells, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("MESG", "Description"):
                    ApplyName(mod.Messages, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Description?.String ?? "", static (item, value) => item.Description = value, result);
                    break;
                case ("QUST", "Name"):
                    ApplyName(mod.Quests, row, static item => item.FormKey.IDString(), static item => item.EditorID,
                        static item => item.Name?.String ?? "", static (item, value) => item.Name = value, result);
                    break;
                default:
                    result.Unsupported.Add(Describe(row, $"unsupported Fallout 4 field {EffectiveField(row)}"));
                    break;
            }
        }

        if (!dryRun && result.Missing.Count == 0 && result.Unsupported.Count == 0)
        {
            mod.BeginWrite
                .ToPath(outputPlugin)
                .WithLoadOrderFromHeaderMasters()
                .WithNoDataFolder()
                .NoModKeySync()
                .WithUtf8Encoding()
                .WithMastersListContent(MastersListContentOption.NoCheck)
                .Write();
        }
        else if (dryRun)
        {
            result.Skipped.Add("Dry run: plugin write skipped.");
        }
        else
        {
            result.Skipped.Add("Plugin write skipped because one or more rows failed closed.");
        }
        return result;
    }

    private static void ApplyName<TRecord>(
        IEnumerable<TRecord> records,
        TranslationRow row,
        Func<TRecord, string> formId,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source,
        Action<TRecord, string> assign,
        AdapterResult result)
        where TRecord : class
    {
        var record = records.FirstOrDefault(item => MatchesIdentity(row, formId(item), editorId(item)));
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
        result.Applied.Add(Describe(row, EffectiveField(row)));
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

    private static bool MatchesIdentity(TranslationRow row, string formId, string? editorId)
    {
        if (!SameFormId(row.FormId, formId))
        {
            return false;
        }
        return string.IsNullOrWhiteSpace(row.EditorId)
            || string.Equals(row.EditorId, editorId ?? "", StringComparison.OrdinalIgnoreCase);
    }

    private static bool SameFormId(string? left, string? right) =>
        string.Equals(NormalizeFormId(left), NormalizeFormId(right), StringComparison.OrdinalIgnoreCase);

    private static string NormalizeFormId(string? value)
    {
        var trimmed = (value ?? "").Trim();
        if (trimmed.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
        {
            trimmed = trimmed[2..];
        }
        trimmed = trimmed.ToUpperInvariant();
        if (trimmed.Length > 6)
        {
            trimmed = trimmed[^6..];
        }
        return trimmed.PadLeft(6, '0');
    }

    private static string EffectiveField(TranslationRow row)
    {
        if (!string.IsNullOrWhiteSpace(row.FieldPath))
        {
            return row.FieldPath;
        }
        return row.SubrecordType switch
        {
            "FULL" => "Name",
            "DESC" or "DNAM" => "Description",
            _ => row.SubrecordType,
        };
    }

    private static string Describe(TranslationRow row, string action) =>
        $"{row.RecordType} {row.FormId} {EffectiveField(row)} {row.EditorId}: {action}";

    private static bool IsLocalized(string inputPlugin)
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
