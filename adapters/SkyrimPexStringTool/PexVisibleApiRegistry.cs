using System.Security.Cryptography;
using System.Text.Json;

internal sealed record PexVisibleArgumentRule(
    int Index,
    string SemanticRole,
    string Classification,
    string VisibilityBasis);

internal sealed record PexVisibleApiRule(
    string Callee,
    HashSet<string> OpcodeForms,
    IReadOnlyDictionary<int, PexVisibleArgumentRule> Arguments);

internal sealed class PexVisibleApiRegistry
{
    private const int SupportedSchemaVersion = 1;
    private static readonly HashSet<string> AllowedRootFields =
    [
        "schema_version",
        "game_id",
        "literal_policy",
        "unmatched_classification",
        "dynamic_argument_classification",
        "apis",
    ];
    private static readonly HashSet<string> AllowedApiFields =
    [
        "callee",
        "opcode_forms",
        "arguments",
        "evidence",
    ];
    private static readonly HashSet<string> AllowedArgumentFields =
    [
        "index",
        "semantic_role",
        "classification",
    ];
    private static readonly HashSet<string> AllowedOpcodes =
    [
        "CALLMETHOD",
        "CALLPARENT",
        "CALLSTATIC",
    ];

    private readonly Dictionary<string, PexVisibleApiRule> _apis;

    private PexVisibleApiRegistry(
        string gameId,
        string sourcePath,
        string sourceSha256,
        Dictionary<string, PexVisibleApiRule> apis)
    {
        GameId = gameId;
        SourcePath = sourcePath;
        SourceSha256 = sourceSha256;
        _apis = apis;
    }

    public string GameId { get; }
    public string SourcePath { get; }
    public string SourceSha256 { get; }

    public static PexVisibleApiRegistry? LoadForGame(
        string gameId,
        string? explicitRegistryPath = null)
    {
        if (!string.Equals(gameId, "fallout4", StringComparison.Ordinal))
        {
            return null;
        }
        var path = string.IsNullOrWhiteSpace(explicitRegistryPath)
            ? ResolveRegistryPath(gameId)
            : Path.GetFullPath(explicitRegistryPath);
        if (!File.Exists(path))
        {
            throw new FileNotFoundException($"PEX visible API registry does not exist: {path}");
        }
        return Load(path, gameId);
    }

    public bool TryGetArgument(
        string callee,
        string opcode,
        int semanticArgumentIndex,
        out PexVisibleArgumentRule? rule)
    {
        rule = null;
        if (!_apis.TryGetValue(callee, out var api)
            || !api.OpcodeForms.Contains(opcode)
            || !api.Arguments.TryGetValue(semanticArgumentIndex, out var found))
        {
            return false;
        }
        rule = found;
        return true;
    }

    private static PexVisibleApiRegistry Load(string path, string expectedGameId)
    {
        var bytes = File.ReadAllBytes(path);
        using var document = JsonDocument.Parse(bytes);
        var root = document.RootElement;
        RequireObject(root, "PEX visible API registry");
        RequireExactFields(root, AllowedRootFields, "PEX visible API registry");
        if (root.GetProperty("schema_version").GetInt32() != SupportedSchemaVersion)
        {
            throw new InvalidDataException("Unsupported PEX visible API registry schema_version.");
        }
        var gameId = RequireText(root, "game_id", "PEX visible API registry");
        if (!string.Equals(gameId, expectedGameId, StringComparison.Ordinal))
        {
            throw new InvalidDataException(
                $"PEX visible API registry game_id '{gameId}' does not match '{expectedGameId}'.");
        }
        if (!string.Equals(RequireText(root, "literal_policy", "PEX visible API registry"), "direct_only", StringComparison.Ordinal)
            || !string.Equals(RequireText(root, "unmatched_classification", "PEX visible API registry"), "manual_review", StringComparison.Ordinal)
            || !string.Equals(RequireText(root, "dynamic_argument_classification", "PEX visible API registry"), "manual_review", StringComparison.Ordinal))
        {
            throw new InvalidDataException("PEX visible API registry fallback policy must fail closed.");
        }

        var apisElement = root.GetProperty("apis");
        if (apisElement.ValueKind != JsonValueKind.Array || apisElement.GetArrayLength() == 0)
        {
            throw new InvalidDataException("PEX visible API registry apis must be a non-empty array.");
        }
        var apis = new Dictionary<string, PexVisibleApiRule>(StringComparer.OrdinalIgnoreCase);
        var classifications = new HashSet<string>(StringComparer.Ordinal);
        foreach (var apiElement in apisElement.EnumerateArray())
        {
            RequireObject(apiElement, "PEX visible API entry");
            RequireExactFields(apiElement, AllowedApiFields, "PEX visible API entry");
            var callee = RequireText(apiElement, "callee", "PEX visible API entry");
            var opcodeValues = ReadUniqueTextArray(apiElement.GetProperty("opcode_forms"), "opcode_forms");
            var opcodes = opcodeValues.ToHashSet(StringComparer.Ordinal);
            if (opcodes.Any(opcode => !AllowedOpcodes.Contains(opcode)))
            {
                throw new InvalidDataException($"PEX visible API '{callee}' has an unsupported opcode.");
            }
            var evidence = ReadUniqueTextArray(apiElement.GetProperty("evidence"), "evidence");
            var argumentElements = apiElement.GetProperty("arguments");
            if (argumentElements.ValueKind != JsonValueKind.Array || argumentElements.GetArrayLength() == 0)
            {
                throw new InvalidDataException($"PEX visible API '{callee}' requires argument rules.");
            }
            var arguments = new Dictionary<int, PexVisibleArgumentRule>();
            foreach (var argumentElement in argumentElements.EnumerateArray())
            {
                RequireObject(argumentElement, "PEX visible API argument");
                RequireExactFields(argumentElement, AllowedArgumentFields, "PEX visible API argument");
                var index = argumentElement.GetProperty("index").GetInt32();
                if (index < 0)
                {
                    throw new InvalidDataException($"PEX visible API '{callee}' has a negative argument index.");
                }
                var role = RequireText(argumentElement, "semantic_role", "PEX visible API argument");
                var classification = RequireText(argumentElement, "classification", "PEX visible API argument");
                if (classification is not ("visible" or "protected"))
                {
                    throw new InvalidDataException($"PEX visible API '{callee}' has an unsafe classification.");
                }
                classifications.Add(classification);
                var basis = $"registry:{Path.GetFileName(path)}#{callee}[{index}]/{evidence[0]}";
                if (!arguments.TryAdd(index, new PexVisibleArgumentRule(index, role, classification, basis)))
                {
                    throw new InvalidDataException($"PEX visible API '{callee}' repeats argument index {index}.");
                }
            }
            if (!apis.TryAdd(callee, new PexVisibleApiRule(callee, opcodes, arguments)))
            {
                throw new InvalidDataException($"Duplicate PEX visible API callee '{callee}'.");
            }
        }
        if (!classifications.SetEquals(["visible", "protected"]))
        {
            throw new InvalidDataException("PEX visible API registry must contain visible and protected rules.");
        }
        return new PexVisibleApiRegistry(
            gameId,
            path,
            Convert.ToHexString(SHA256.HashData(bytes)),
            apis);
    }

    private static string ResolveRegistryPath(string gameId)
    {
        foreach (var start in new[] { AppContext.BaseDirectory, Directory.GetCurrentDirectory() })
        {
            var current = new DirectoryInfo(Path.GetFullPath(start));
            for (var depth = 0; current is not null && depth < 12; depth++, current = current.Parent)
            {
                var candidate = Path.Combine(current.FullName, "config", "pex_visible_apis", $"{gameId}.json");
                if (File.Exists(candidate))
                {
                    return Path.GetFullPath(candidate);
                }
            }
        }
        throw new FileNotFoundException(
            $"Could not locate the versioned PEX visible API registry for {gameId}.");
    }

    private static void RequireObject(JsonElement element, string label)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException($"{label} must be an object.");
        }
    }

    private static void RequireExactFields(JsonElement element, HashSet<string> allowed, string label)
    {
        var actual = element.EnumerateObject().Select(property => property.Name).ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(allowed))
        {
            throw new InvalidDataException($"{label} fields do not match the supported schema.");
        }
    }

    private static string RequireText(JsonElement parent, string property, string label)
    {
        var element = parent.GetProperty(property);
        if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(element.GetString()))
        {
            throw new InvalidDataException($"{label}.{property} must be non-empty text.");
        }
        return element.GetString()!.Trim();
    }

    private static IReadOnlyList<string> ReadUniqueTextArray(JsonElement element, string label)
    {
        if (element.ValueKind != JsonValueKind.Array || element.GetArrayLength() == 0)
        {
            throw new InvalidDataException($"PEX visible API {label} must be a non-empty array.");
        }
        var values = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in element.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(item.GetString()))
            {
                throw new InvalidDataException($"PEX visible API {label} contains an empty value.");
            }
            var value = item.GetString()!.Trim();
            if (!seen.Add(value))
            {
                throw new InvalidDataException($"PEX visible API {label} contains a duplicate value.");
            }
            values.Add(value);
        }
        return values;
    }
}
