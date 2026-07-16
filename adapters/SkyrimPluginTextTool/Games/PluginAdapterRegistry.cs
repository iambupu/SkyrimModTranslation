internal static class PluginAdapterRegistry
{
    private static readonly IPluginTextAdapter Skyrim = new SkyrimPluginAdapter();
    private static readonly IPluginTextAdapter Fallout4 = new Fallout4PluginTextAdapter();

    public static IPluginTextAdapter Resolve(string mutagenRelease) => mutagenRelease switch
    {
        "SkyrimSE" => Skyrim,
        "Fallout4" => Fallout4,
        _ => throw new ArgumentException($"Unknown Mutagen release: {mutagenRelease}"),
    };

    public static IPluginTextAdapter ResolveForIdentity(
        string mutagenRelease,
        string gameId)
    {
        var adapter = Resolve(mutagenRelease);
        if (!string.Equals(adapter.GameId, gameId, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                $"Game identity '{gameId}' is incompatible with Mutagen release "
                + $"'{adapter.MutagenRelease}', which requires '{adapter.GameId}'.");
        }
        return adapter;
    }
}
