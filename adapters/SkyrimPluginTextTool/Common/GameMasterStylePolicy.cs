using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Plugins;

internal static class GameMasterStylePolicy
{
    private const string ResourceName = "SkyrimPluginTextTool.Config.plugin_master_styles.json";
    private static readonly IReadOnlyDictionary<string, IReadOnlySet<ModKey>> KnownFullMasters =
        LoadKnownFullMasters();

    public static bool IsKnownFullMaster(string gameId, ModKey master) =>
        KnownFullMasters.TryGetValue(gameId, out var masters) && masters.Contains(master);

    private static IReadOnlyDictionary<string, IReadOnlySet<ModKey>> LoadKnownFullMasters()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(ResourceName)
            ?? throw new InvalidDataException(
                $"master_style_conflict: embedded known-full master policy is missing: {ResourceName}");
        MasterStylePolicyDocument document;
        try
        {
            document = JsonSerializer.Deserialize<MasterStylePolicyDocument>(stream)
                ?? throw new InvalidDataException(
                    "master_style_conflict: embedded known-full master policy is empty");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException(
                $"master_style_conflict: embedded known-full master policy is invalid: {exception.Message}",
                exception);
        }
        if (document.SchemaVersion != 1 || document.KnownFullMasters.Count == 0)
        {
            throw new InvalidDataException(
                "master_style_conflict: embedded known-full master policy must use schema_version 1");
        }

        var policy = new Dictionary<string, IReadOnlySet<ModKey>>(StringComparer.Ordinal);
        foreach (var (gameId, names) in document.KnownFullMasters)
        {
            if (string.IsNullOrWhiteSpace(gameId) || names is not { Count: > 0 })
            {
                throw new InvalidDataException(
                    "master_style_conflict: embedded known-full master policy contains an invalid game entry");
            }
            var masters = new HashSet<ModKey>();
            foreach (var name in names)
            {
                if (!ModKey.TryFromNameAndExtension(name, out var master)
                    || master.Type == ModType.Light
                    || !masters.Add(master))
                {
                    throw new InvalidDataException(
                        $"master_style_conflict: embedded known-full master policy contains an invalid master for {gameId}");
                }
            }
            policy.Add(gameId, masters);
        }
        return policy;
    }

    private sealed class MasterStylePolicyDocument
    {
        [JsonPropertyName("schema_version")]
        public int SchemaVersion { get; init; }

        [JsonPropertyName("known_full_masters")]
        public Dictionary<string, List<string>> KnownFullMasters { get; init; } = [];
    }
}
