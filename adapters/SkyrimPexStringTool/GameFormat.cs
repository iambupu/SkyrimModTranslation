using Mutagen.Bethesda;

internal static class GameFormat
{
    public static GameCategory ResolveCategory(string pexCategory) => pexCategory switch
    {
        "Skyrim" => GameCategory.Skyrim,
        "Fallout4" => GameCategory.Fallout4,
        _ => throw new ArgumentException($"Unsupported PEX category: {pexCategory}"),
    };
}
