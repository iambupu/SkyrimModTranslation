using Mutagen.Bethesda.Fallout4;

internal static class Fallout4PluginTraits
{
    private const byte UnsupportedLightFormIdMarker = 0xFE;

    public static PluginTraits Inspect(
        string inputPlugin,
        Fallout4Mod mod,
        IEnumerable<uint>? majorRecordFormIds = null)
    {
        var flags = mod.ModHeader.Flags;
        return new(
            flags.HasFlag(Fallout4ModHeader.HeaderFlag.Localized),
            string.Equals(Path.GetExtension(inputPlugin), ".esl", StringComparison.OrdinalIgnoreCase),
            flags.HasFlag(Fallout4ModHeader.HeaderFlag.Small),
            majorRecordFormIds?.Any(IsUnsupportedLightFormId));
    }

    private static bool IsUnsupportedLightFormId(uint rawFormId) =>
        rawFormId >> 24 == UnsupportedLightFormIdMarker;
}
