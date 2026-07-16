internal static class PluginFieldContract
{
    private static readonly IReadOnlyDictionary<(string Record, string Subrecord), string> SkyrimFields =
        new Dictionary<(string, string), string>
        {
            [("MGEF", "FULL")] = "Name",
            [("MGEF", "DNAM")] = "Description",
            [("SPEL", "FULL")] = "Name",
            [("SPEL", "DESC")] = "Description",
            [("ARMO", "FULL")] = "Name",
            [("ARMO", "DESC")] = "Description",
            [("WEAP", "FULL")] = "Name",
            [("CELL", "FULL")] = "Name",
            [("CLAS", "FULL")] = "Name",
            [("CLFM", "FULL")] = "Name",
            [("PERK", "FULL")] = "Name",
            [("PERK", "DESC")] = "Description",
            [("FACT", "FULL")] = "Name",
            [("ENCH", "FULL")] = "Name",
            [("CONT", "FULL")] = "Name",
            [("MISC", "FULL")] = "Name",
            [("ALCH", "FULL")] = "Name",
            [("ALCH", "DESC")] = "Description",
            [("WRLD", "FULL")] = "Name",
            [("DIAL", "FULL")] = "Name",
            [("INFO", "RNAM")] = "Prompt",
            [("INFO", "NAM1")] = "Responses[].Text",
            [("QUST", "FULL")] = "Name",
            [("QUST", "DESC")] = "Description",
            [("MESG", "DESC")] = "Description",
            [("MESG", "FULL")] = "Name",
            [("MESG", "ITXT")] = "MenuButtons[].Text",
        };

    private static readonly IReadOnlyDictionary<(string Record, string Subrecord), string> Fallout4Fields =
        Fallout4PluginFieldRegistry.ContractFields;

    public static bool TryValidate(string game, TranslationRow row, out string reason)
    {
        if (row.SchemaVersion != 2)
        {
            reason = $"unsupported schema_version={row.SchemaVersion}";
            return false;
        }
        if (string.IsNullOrEmpty(row.Source))
        {
            reason = "schema v2 source must be non-empty";
            return false;
        }

        var fields = FieldsFor(game);
        if (fields is null)
        {
            reason = $"unsupported game identity {game}";
            return false;
        }
        if (!fields.TryGetValue((row.RecordType, row.SubrecordType), out var expected))
        {
            reason = $"unsupported {game} field combination {row.RecordType}/{row.SubrecordType}";
            return false;
        }
        if (!string.Equals(row.FieldPath, expected, StringComparison.Ordinal))
        {
            reason = $"field_path mismatch for {row.RecordType}/{row.SubrecordType}: expected {expected}, found {row.FieldPath}";
            return false;
        }
        if (!string.Equals(row.Writeback, "supported", StringComparison.Ordinal))
        {
            reason = $"field is not marked supported for writeback: {row.Writeback}";
            return false;
        }
        if (RequiresOccurrenceIndex(game, row.RecordType, row.SubrecordType)
            && row.OccurrenceIndex is not >= 0)
        {
            reason = $"{row.RecordType}/{row.SubrecordType} requires occurrence_index";
            return false;
        }
        reason = string.Empty;
        return true;
    }

    public static bool TryGetFieldPath(
        string game,
        string recordType,
        string subrecordType,
        out string fieldPath)
    {
        var fields = FieldsFor(game);
        if (fields is not null
            && fields.TryGetValue((recordType, subrecordType), out var value))
        {
            fieldPath = value;
            return true;
        }
        fieldPath = string.Empty;
        return false;
    }

    public static bool RequiresOccurrenceIndex(
        string game,
        string recordType,
        string subrecordType) =>
        game == "skyrim-se"
        && (recordType, subrecordType) is ("INFO", "NAM1") or ("MESG", "ITXT");

    private static IReadOnlyDictionary<(string Record, string Subrecord), string>? FieldsFor(
        string game) => game switch
        {
            "skyrim-se" => SkyrimFields,
            "fallout4" => Fallout4Fields,
            _ => null,
        };
}
