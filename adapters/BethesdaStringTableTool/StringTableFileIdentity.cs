public sealed record StringTableFileIdentity(
    string PluginBasename,
    string Language,
    StringTableType TableType)
{
    public static StringTableFileIdentity Parse(string path, string expectedLanguage)
    {
        var tableType = StringTableCodec.TypeFromPath(path);
        var stem = Path.GetFileNameWithoutExtension(path);
        var language = RequireLanguageToken(expectedLanguage);
        var suffix = $"_{language}";
        if (!stem.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)
            || stem.Length == suffix.Length)
        {
            throw new InvalidDataException(
                $"String-table filename must end with the active source-language token '{suffix}'.");
        }
        return new StringTableFileIdentity(
            stem[..^suffix.Length],
            stem[^language.Length..],
            tableType);
    }

    public string FilenameForLanguage(string language)
    {
        var token = RequireLanguageToken(language);
        return $"{PluginBasename}_{token}.{StringTableCodec.TypeName(TableType)}";
    }

    private static string RequireLanguageToken(string language)
    {
        var token = language.Trim();
        if (token.Length == 0
            || token.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
            || token.StartsWith('_')
            || token.EndsWith('_')
            || token.Split('_').Any(part => part.Length == 0 || !part.All(char.IsLetterOrDigit)))
        {
            throw new InvalidDataException("String-table language token is invalid.");
        }
        return token;
    }
}
