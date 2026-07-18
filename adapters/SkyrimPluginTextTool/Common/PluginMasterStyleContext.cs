using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Plugins.Masters;
using Mutagen.Bethesda.Plugins.Records;
using Noggog;

internal sealed record ResolvedMasterStyle(
    ModKey ModKey,
    MasterStyle Style,
    string EvidenceSource,
    string? RelativePath,
    string? Sha256);

internal sealed class PluginMasterStyleContext
{
    private readonly Cache<IModMasterStyledGetter, ModKey>? _lookup;
    private readonly IReadOnlySeparatedMasterPackage? _masterPackage;
    private readonly IReadOnlyDictionary<ModKey, ResolvedMasterStyle> _styles;

    private PluginMasterStyleContext(
        bool required,
        PluginHeaderMetadata header,
        GameRelease gameRelease,
        IReadOnlyDictionary<ModKey, ResolvedMasterStyle> styles,
        Cache<IModMasterStyledGetter, ModKey>? lookup,
        IReadOnlySeparatedMasterPackage? masterPackage,
        string contextPath)
    {
        Required = required;
        Header = header;
        GameRelease = gameRelease;
        _styles = styles;
        _lookup = lookup;
        _masterPackage = masterPackage;
        ContextPath = contextPath;
    }

    public bool Required { get; }
    public PluginHeaderMetadata Header { get; }
    public GameRelease GameRelease { get; }
    public string ContextPath { get; }
    public IReadOnlySeparatedMasterPackage? MasterPackage => _masterPackage;
    public IReadOnlyCollection<ResolvedMasterStyle> Styles => _styles.Values.ToArray();
    public IReadOnlyCache<IModMasterStyledGetter, ModKey>? MasterFlagsLookup => _lookup;
    public KeyedMasterStyle[] KnownMasters => _styles.Values
        .Where(item => item.ModKey != Header.ModKey)
        .Select(static item => new KeyedMasterStyle(item.ModKey, item.Style))
        .ToArray();

    public static PluginMasterStyleContext Resolve(
        string projectRoot,
        string inputPlugin,
        string gameId,
        string? explicitManifestPath)
    {
        var root = Path.GetFullPath(projectRoot);
        var input = Path.GetFullPath(inputPlugin);
        EnsureInside(root, input, "input plugin");
        var header = PluginHeaderMetadata.Read(input);
        var gameRelease = gameId switch
        {
            "skyrim-se" => GameRelease.SkyrimSE,
            "fallout4" => GameRelease.Fallout4,
            _ => throw new InvalidDataException($"unsupported game_id for master-style context: {gameId}"),
        };

        var contextPath = ResolveContextPath(root, input, header.ModKey);
        var manifestPath = ResolveManifestPath(root, contextPath, explicitManifestPath);
        var manifest = manifestPath is null
            ? null
            : ReadManifest(manifestPath, gameId, header.ModKey);
        var manifestEntries = new List<(ModKey ModKey, MasterStyle Style, string EvidenceSource)>();
        if (manifest is not null)
        {
            foreach (var entry in manifest.Masters)
            {
                if (!ModKey.TryFromNameAndExtension(entry.ModKey, out var manifestModKey))
                {
                    throw new InvalidDataException(
                        $"master-style manifest contains an invalid mod_key: {entry.ModKey}");
                }
                if (string.IsNullOrWhiteSpace(entry.EvidenceSource))
                {
                    throw new InvalidDataException(
                        $"master-style manifest evidence_source is empty for {manifestModKey}");
                }
                manifestEntries.Add((
                    manifestModKey,
                    ParseStyle(entry.MasterStyle),
                    entry.EvidenceSource));
            }
        }

        var localMasters = new List<(ModKey Master, string Path, PluginHeaderMetadata Header)>();
        foreach (var master in header.Masters)
        {
            var localPath = Path.Combine(Path.GetDirectoryName(input)!, master.FileName.String);
            if (!File.Exists(localPath)) continue;
            EnsureInside(root, localPath, $"local master {master}");
            var localHeader = PluginHeaderMetadata.Read(localPath);
            if (localHeader.ModKey != master)
            {
                throw new InvalidDataException(
                    $"local master identity mismatch: expected {master}, found {localHeader.ModKey}");
            }
            localMasters.Add((master, localPath, localHeader));
        }

        var rawFormIds = PluginBinaryInvariant.ReadRawMajorRecordFormIds(input);
        var invalidRawFormId = rawFormIds.FirstOrDefault(raw => raw >> 24 > header.Masters.Count);
        if (invalidRawFormId != 0)
        {
            var masterIndex = invalidRawFormId >> 24;
            var reason = masterIndex == 0xFE
                ? "raw 0xFE/load-order FormID cannot authorize plugin writeback; use the plugin-local FormID with canonical owner evidence"
                : $"form_id master index {masterIndex} exceeds header master count {header.Masters.Count}";
            throw new InvalidDataException(reason);
        }
        var required = header.IsSmall
            || header.Masters.Any(static master => master.Type == ModType.Light)
            || rawFormIds.Any(static raw => raw >> 24 == 0xFE)
            || localMasters.Any(static item => item.Header.IsSmall)
            || manifestEntries.Any(static entry => entry.Style == MasterStyle.Small);
        if (!required)
        {
            return new(false, header, gameRelease, new Dictionary<ModKey, ResolvedMasterStyle>(), null, null, string.Empty);
        }

        var candidates = new Dictionary<ModKey, List<StyleCandidate>>();
        var inputRelative = Relative(root, input);
        AddCandidate(
            candidates,
            header.ModKey,
            header.IsSmall ? MasterStyle.Small : MasterStyle.Full,
            $"workspace-header:{inputRelative}",
            inputRelative,
            Sha256(input));

        if (manifestEntries.Count > 0)
        {
            var manifestRelative = Relative(root, manifestPath!);
            var manifestSha256 = Sha256(manifestPath!);
            foreach (var entry in manifestEntries)
            {
                AddCandidate(
                    candidates,
                    entry.ModKey,
                    entry.Style,
                    $"manifest:{entry.EvidenceSource}",
                    manifestRelative,
                    manifestSha256);
            }
        }

        foreach (var master in header.Masters.Where(static item => item.Type == ModType.Light))
        {
            AddCandidate(
                candidates,
                master,
                MasterStyle.Small,
                "extension:.esl",
                null,
                null);
        }
        foreach (var localMaster in localMasters)
        {
            var relative = Relative(root, localMaster.Path);
            AddCandidate(
                candidates,
                localMaster.Master,
                localMaster.Header.IsSmall ? MasterStyle.Small : MasterStyle.Full,
                $"workspace-header:{relative}",
                relative,
                Sha256(localMaster.Path));
        }

        var expectedOwners = header.Masters.Prepend(header.ModKey).ToHashSet();
        var unexpected = candidates.Keys.Where(owner => !expectedOwners.Contains(owner)).ToArray();
        if (unexpected.Length > 0)
        {
            throw new InvalidDataException(
                $"master-style manifest contains owners outside the plugin header: {string.Join(", ", unexpected)}");
        }

        var resolved = new Dictionary<ModKey, ResolvedMasterStyle>();
        foreach (var owner in expectedOwners)
        {
            if (!candidates.TryGetValue(owner, out var ownerCandidates) || ownerCandidates.Count == 0)
            {
                throw new InvalidDataException(
                    $"unknown master style for {owner}; provide a workspace-local plugin header or master-style manifest");
            }
            var styles = ownerCandidates.Select(static item => item.Style).Distinct().ToArray();
            if (styles.Length != 1)
            {
                var evidence = string.Join(", ", ownerCandidates.Select(static item =>
                    $"{StyleName(item.Style)} from {item.Source}"));
                throw new InvalidDataException($"conflicting master style evidence for {owner}: {evidence}");
            }
            var style = styles[0];
            var sources = string.Join("; ", ownerCandidates.Select(static item => item.Source).Distinct());
            var inspected = ownerCandidates.FirstOrDefault(static item => item.RelativePath is not null);
            resolved.Add(
                owner,
                new(
                    owner,
                    style,
                    sources,
                    inspected?.RelativePath,
                    inspected?.Sha256));
        }

        var lookup = new Cache<IModMasterStyledGetter, ModKey>(
            static item => item.ModKey,
            EqualityComparer<ModKey>.Default);
        lookup.Set(resolved.Values.Select(static item =>
            (IModMasterStyledGetter)new KeyedMasterStyle(item.ModKey, item.Style)));
        var references = new MasterReferenceCollection(
            header.ModKey,
            header.Masters.Select(static master =>
                (IMasterReferenceGetter)new MasterReference { Master = master }));
        var current = resolved[header.ModKey];
        var package = SeparatedMasterPackage.Factory(
            gameRelease,
            header.ModKey,
            current.Style,
            references,
            lookup);
        Directory.CreateDirectory(Path.GetDirectoryName(contextPath)!);
        WriteContext(contextPath, gameId, inputRelative, input, current, header.Masters, resolved);
        return new(true, header, gameRelease, resolved, lookup, package, contextPath);
    }

    public bool TryGetStyle(FormKey formKey, out ResolvedMasterStyle style) =>
        TryGetStyle(formKey.ModKey, out style);

    public bool TryGetStyle(ModKey modKey, out ResolvedMasterStyle style) =>
        _styles.TryGetValue(modKey, out style!);

    private static string ResolveContextPath(string root, string input, ModKey modKey)
    {
        var relative = Path.GetRelativePath(root, input);
        var parts = relative.Split(
            [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
            StringSplitOptions.RemoveEmptyEntries);
        var modName = parts.Length >= 3
            && string.Equals(parts[0], "work", StringComparison.OrdinalIgnoreCase)
            && string.Equals(parts[1], "extracted_mods", StringComparison.OrdinalIgnoreCase)
                ? parts[2]
                : Path.GetFileNameWithoutExtension(input);
        return Path.Combine(
            root,
            "work",
            "plugin_context",
            modName,
            $"{modKey.FileName.String}.resolved-master-styles.json");
    }

    private static string? ResolveManifestPath(
        string root,
        string contextPath,
        string? explicitManifestPath)
    {
        if (!string.IsNullOrWhiteSpace(explicitManifestPath))
        {
            var explicitPath = Path.GetFullPath(explicitManifestPath);
            EnsureInside(root, explicitPath, "master-style manifest");
            if (!string.Equals(Path.GetExtension(explicitPath), ".json", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("master-style manifest must be a JSON file");
            }
            if (!File.Exists(explicitPath))
            {
                throw new FileNotFoundException("master-style manifest does not exist", explicitPath);
            }
            return explicitPath;
        }

        var defaultPath = contextPath.Replace(
            ".resolved-master-styles.json",
            ".master-styles.json",
            StringComparison.Ordinal);
        return File.Exists(defaultPath) ? defaultPath : null;
    }

    private static MasterStyleManifest ReadManifest(
        string path,
        string gameId,
        ModKey plugin)
    {
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        var manifest = JsonSerializer.Deserialize<MasterStyleManifest>(File.ReadAllText(path), options)
            ?? throw new InvalidDataException("master-style manifest is empty");
        if (manifest.SchemaVersion != 1)
        {
            throw new InvalidDataException(
                $"unsupported master-style manifest schema_version={manifest.SchemaVersion}");
        }
        if (!string.Equals(manifest.GameId, gameId, StringComparison.Ordinal))
        {
            throw new InvalidDataException(
                $"master-style manifest game_id {manifest.GameId} does not match {gameId}");
        }
        if (!string.Equals(manifest.Plugin, plugin.FileName.String, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"master-style manifest plugin {manifest.Plugin} does not match {plugin}");
        }
        if (manifest.Masters.Count == 0)
        {
            throw new InvalidDataException("master-style manifest masters must not be empty");
        }
        return manifest;
    }

    private static MasterStyle ParseStyle(string value) => value.Trim().ToLowerInvariant() switch
    {
        "full" => MasterStyle.Full,
        "light" or "small" => MasterStyle.Small,
        _ => throw new InvalidDataException($"unsupported master_style: {value}"),
    };

    private static void AddCandidate(
        IDictionary<ModKey, List<StyleCandidate>> candidates,
        ModKey modKey,
        MasterStyle style,
        string source,
        string? relativePath,
        string? sha256)
    {
        if (!candidates.TryGetValue(modKey, out var values))
        {
            values = [];
            candidates.Add(modKey, values);
        }
        values.Add(new(style, source, relativePath, sha256));
    }

    private static void WriteContext(
        string contextPath,
        string gameId,
        string inputRelative,
        string inputPlugin,
        ResolvedMasterStyle current,
        IReadOnlyList<ModKey> masters,
        IReadOnlyDictionary<ModKey, ResolvedMasterStyle> resolved)
    {
        var payload = new
        {
            schema_version = 1,
            game_id = gameId,
            plugin = current.ModKey.FileName.String,
            input_path = inputRelative.Replace('\\', '/'),
            input_sha256 = Sha256(inputPlugin),
            current_style = StyleName(current.Style),
            current_evidence_source = current.EvidenceSource,
            current_inspected_path = current.RelativePath?.Replace('\\', '/'),
            current_inspected_sha256 = current.Sha256,
            masters = masters.Select(master =>
            {
                var item = resolved[master];
                return new
                {
                    mod_key = master.FileName.String,
                    master_style = StyleName(item.Style),
                    evidence_source = item.EvidenceSource,
                    inspected_path = item.RelativePath?.Replace('\\', '/'),
                    inspected_sha256 = item.Sha256,
                };
            }),
        };
        var temporary = AtomicPluginOutput.CreateTemporaryPath(contextPath);
        try
        {
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, contextPath, overwrite: true);
        }
        catch
        {
            if (File.Exists(temporary)) File.Delete(temporary);
            throw;
        }
    }

    internal static string StyleName(MasterStyle style) => style switch
    {
        MasterStyle.Full => "full",
        MasterStyle.Small => "light",
        MasterStyle.Medium => "medium",
        _ => throw new InvalidDataException($"unsupported master style: {style}"),
    };

    private static string Relative(string root, string path) =>
        Path.GetRelativePath(root, path).Replace('\\', '/');

    private static string Sha256(string path) =>
        Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path)));

    private static void EnsureInside(string root, string path, string label)
    {
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var pathFull = Path.GetFullPath(path);
        if (!string.Equals(rootFull, pathFull, StringComparison.OrdinalIgnoreCase)
            && !pathFull.StartsWith(rootFull + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException($"{label} is outside project root: {pathFull}");
        }
    }

    private sealed record StyleCandidate(
        MasterStyle Style,
        string Source,
        string? RelativePath,
        string? Sha256);

    private sealed class MasterStyleManifest
    {
        [JsonPropertyName("schema_version")]
        public int SchemaVersion { get; set; }

        [JsonPropertyName("game_id")]
        public string GameId { get; set; } = string.Empty;

        [JsonPropertyName("plugin")]
        public string Plugin { get; set; } = string.Empty;

        [JsonPropertyName("masters")]
        public List<MasterStyleManifestEntry> Masters { get; set; } = [];
    }

    private sealed class MasterStyleManifestEntry
    {
        [JsonPropertyName("mod_key")]
        public string ModKey { get; set; } = string.Empty;

        [JsonPropertyName("master_style")]
        public string MasterStyle { get; set; } = string.Empty;

        [JsonPropertyName("evidence_source")]
        public string EvidenceSource { get; set; } = string.Empty;
    }
}
