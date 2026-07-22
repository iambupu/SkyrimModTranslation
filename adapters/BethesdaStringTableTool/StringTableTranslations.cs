using System.Text;
using System.Text.Json;

internal sealed record StringTableTranslationRow(
    int SchemaVersion,
    string GameId,
    string PluginBasename,
    string TableType,
    string SourceLanguage,
    uint StringId,
    string Source,
    string Target,
    string SourceTablePath,
    string SourceTableSha256);

internal sealed record StringTableTranslationSet(
    IReadOnlyList<StringTableTranslationRow> Rows,
    IReadOnlyDictionary<uint, string> Replacements);

internal static class StringTableTranslations
{
    internal const int SchemaVersion = 2;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    internal static StringTableTranslationSet LoadAndValidate(
        string translationPath,
        string expectedGameId,
        StringTableFileIdentity identity,
        string expectedSourceLanguage,
        string expectedSourcePath,
        string expectedSourceSha256,
        IReadOnlyDictionary<uint, string> sourceValues,
        Encoding encoding)
    {
        var rows = new List<StringTableTranslationRow>();
        var replacements = new Dictionary<uint, string>();
        var seen = new HashSet<uint>();
        var lineNumber = 0;
        foreach (var line in File.ReadLines(translationPath, StrictUtf8))
        {
            lineNumber++;
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }
            JsonDocument document;
            try
            {
                document = JsonDocument.Parse(line);
            }
            catch (JsonException ex)
            {
                throw new InvalidDataException(
                    $"Translation JSONL line {lineNumber} is invalid JSON.",
                    ex);
            }
            using (document)
            {
                var root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object)
                {
                    throw new InvalidDataException(
                        $"Translation JSONL line {lineNumber} must be an object.");
                }
                var row = ParseRow(root, lineNumber);
                ValidateIdentity(
                    row,
                    lineNumber,
                    expectedGameId,
                    identity,
                    expectedSourceLanguage,
                    expectedSourcePath,
                    expectedSourceSha256);
                if (!seen.Add(row.StringId))
                {
                    throw new InvalidDataException(
                        $"Translation JSONL repeats string-table identity for ID {row.StringId}.");
                }
                if (!sourceValues.TryGetValue(row.StringId, out var source))
                {
                    throw new InvalidDataException(
                        $"Translation JSONL references missing string ID {row.StringId}.");
                }
                if (!string.Equals(row.Source, source, StringComparison.Ordinal))
                {
                    throw new InvalidDataException(
                        $"Source drift for string ID {row.StringId}.");
                }
                if (row.Target.Contains('\0'))
                {
                    throw new InvalidDataException(
                        $"Target for string ID {row.StringId} contains a NUL character.");
                }
                if (!string.IsNullOrEmpty(row.Target)
                    && !string.Equals(row.Target, row.Source, StringComparison.Ordinal))
                {
                    try
                    {
                        encoding.GetByteCount(row.Target);
                    }
                    catch (EncoderFallbackException ex)
                    {
                        throw new InvalidDataException(
                            $"Target for string ID {row.StringId} is invalid for encoding {encoding.WebName}.",
                            ex);
                    }
                    replacements.Add(row.StringId, row.Target);
                }
                rows.Add(row);
            }
        }
        return new StringTableTranslationSet(rows, replacements);
    }

    private static StringTableTranslationRow ParseRow(JsonElement root, int lineNumber)
    {
        var schemaVersion = RequireInt32(root, "schema_version", lineNumber);
        var stringId = RequireUInt32(root, "string_id", lineNumber);
        return new StringTableTranslationRow(
            schemaVersion,
            RequireText(root, "game_id", lineNumber),
            RequireText(root, "plugin_basename", lineNumber),
            RequireText(root, "table_type", lineNumber),
            RequireText(root, "source_language", lineNumber),
            stringId,
            RequireString(root, "Source", lineNumber),
            OptionalString(root, "Result", lineNumber),
            RequireText(root, "source_table_path", lineNumber).Replace('\\', '/'),
            RequireText(root, "source_table_sha256", lineNumber));
    }

    private static void ValidateIdentity(
        StringTableTranslationRow row,
        int lineNumber,
        string expectedGameId,
        StringTableFileIdentity identity,
        string expectedSourceLanguage,
        string expectedSourcePath,
        string expectedSourceSha256)
    {
        if (row.SchemaVersion != SchemaVersion)
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} requires schema_version={SchemaVersion}.");
        }
        var expectedType = StringTableCodec.TypeName(identity.TableType);
        if (!string.Equals(row.GameId, expectedGameId, StringComparison.Ordinal)
            || !string.Equals(row.PluginBasename, identity.PluginBasename, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(row.TableType, expectedType, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(row.SourceLanguage, expectedSourceLanguage, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(row.SourceTablePath, expectedSourcePath, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(row.SourceTableSha256, expectedSourceSha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} does not match the active string-table identity.");
        }
    }

    private static string RequireText(JsonElement root, string property, int lineNumber)
    {
        var value = RequireString(root, property, lineNumber);
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} field '{property}' must be non-empty text.");
        }
        return value.Trim();
    }

    private static string RequireString(JsonElement root, string property, int lineNumber)
    {
        if (!root.TryGetProperty(property, out var value) || value.ValueKind != JsonValueKind.String)
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} field '{property}' must be text.");
        }
        return value.GetString() ?? "";
    }

    private static string OptionalString(JsonElement root, string property, int lineNumber)
    {
        if (!root.TryGetProperty(property, out var value))
        {
            return "";
        }
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} field '{property}' must be text.");
        }
        return value.GetString() ?? "";
    }

    private static int RequireInt32(JsonElement root, string property, int lineNumber)
    {
        if (!root.TryGetProperty(property, out var value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt32(out var parsed))
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} field '{property}' must be an integer.");
        }
        return parsed;
    }

    private static uint RequireUInt32(JsonElement root, string property, int lineNumber)
    {
        if (!root.TryGetProperty(property, out var value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetUInt32(out var parsed))
        {
            throw new InvalidDataException(
                $"Translation JSONL line {lineNumber} field '{property}' must be an unsigned 32-bit integer.");
        }
        return parsed;
    }
}
