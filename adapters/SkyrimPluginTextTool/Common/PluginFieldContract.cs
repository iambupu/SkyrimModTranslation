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
        new Dictionary<(string, string), string>
        {
            [("WEAP", "FULL")] = "Name",
            [("ARMO", "FULL")] = "Name",
            [("MISC", "FULL")] = "Name",
            [("ALCH", "FULL")] = "Name",
            [("CELL", "FULL")] = "Name",
            [("WRLD", "FULL")] = "Name",
            [("PERK", "FULL")] = "Name",
            [("PERK", "DESC")] = "Description",
            [("MGEF", "FULL")] = "Name",
            [("MGEF", "DNAM")] = "Description",
            [("SPEL", "FULL")] = "Name",
            [("SPEL", "DESC")] = "Description",
            [("MESG", "DESC")] = "Description",
            [("QUST", "FULL")] = "Name",
        };

    public static bool TryValidate(string game, TranslationRow row, out string reason)
    {
        if (game == "fallout4" && row.SchemaVersion != 2)
        {
            reason = "Fallout 4 writeback requires schema_version=2";
            return false;
        }
        if (game == "skyrim-se" && row.SchemaVersion == 1)
        {
            reason = string.Empty;
            return true;
        }
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

        var fields = game == "fallout4" ? Fallout4Fields : SkyrimFields;
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
        reason = string.Empty;
        return true;
    }
}
