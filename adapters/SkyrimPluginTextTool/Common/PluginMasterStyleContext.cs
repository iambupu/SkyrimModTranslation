using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
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
    string? Sha256,
    bool? SmallFlag);

internal sealed class PluginMasterStyleContext
{
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);
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
        string contextPath,
        bool? referencesLightMaster)
    {
        Required = required;
        Header = header;
        GameRelease = gameRelease;
        _styles = styles;
        _lookup = lookup;
        _masterPackage = masterPackage;
        ContextPath = contextPath;
        ReferencesLightMaster = referencesLightMaster;
    }

    public bool Required { get; }
    public PluginHeaderMetadata Header { get; }
    public GameRelease GameRelease { get; }
    public string ContextPath { get; }
    public bool CurrentPluginLight => Header.IsSmall;
    public bool? ReferencesLightMaster { get; }
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
        string? explicitManifestPath,
        bool requireCompleteMap = false,
        IReadOnlyCollection<uint>? targetRawFormIds = null,
        bool manifestDefinesTargetScope = false)
    {
        var root = Path.GetFullPath(projectRoot);
        var input = Path.GetFullPath(inputPlugin);
        EnsureInside(root, input, "input plugin");
        EnsureRegularSingleLinkFile(root, input, "input plugin", Stale);
        var header = PluginHeaderMetadata.Read(input);
        var gameRelease = gameId switch
        {
            "skyrim-se" => GameRelease.SkyrimSE,
            "fallout4" => GameRelease.Fallout4,
            _ => throw new InvalidDataException($"unsupported game_id for master-style context: {gameId}"),
        };

        var contextPath = ContextPathFor(root, input, header.ModKey);
        var manifestPath = ResolveManifestPath(root, contextPath, explicitManifestPath);
        var manifest = manifestPath is null
            ? null
            : ReadManifest(manifestPath, gameId, header.ModKey);
        var expectedOwners = header.Masters.Prepend(header.ModKey).ToHashSet();
        var targetOwners = targetRawFormIds is null
            ? null
            : ResolveTargetOwners(header, targetRawFormIds);
        var manifestEntries = new List<ManifestStyleEvidence>();
        if (manifest is not null)
        {
            var manifestOwners = new HashSet<ModKey>();
            foreach (var entry in manifest.Masters)
            {
                if (string.IsNullOrWhiteSpace(entry.ModKey)
                    || !ModKey.TryFromNameAndExtension(entry.ModKey, out var manifestModKey))
                {
                    throw Conflict(
                        $"master-style manifest contains an invalid mod_key: {entry.ModKey}");
                }
                if (!manifestOwners.Add(manifestModKey))
                {
                    throw Conflict(
                        $"master-style manifest contains duplicate evidence for {manifestModKey}");
                }
                if (!expectedOwners.Contains(manifestModKey))
                {
                    throw Conflict(
                        $"master-style manifest contains owners outside the plugin header: {manifestModKey}");
                }
            }
            if (manifestDefinesTargetScope && targetOwners is not null)
            {
                targetOwners.RemoveWhere(owner =>
                    owner != header.ModKey && !manifestOwners.Contains(owner));
            }
            foreach (var entry in manifest.Masters)
            {
                _ = ModKey.TryFromNameAndExtension(entry.ModKey, out var manifestModKey);
                if (targetOwners is not null && !targetOwners.Contains(manifestModKey))
                {
                    continue;
                }
                manifestEntries.Add(ReadManifestEvidence(root, manifestModKey, entry));
            }
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
            || manifestEntries.Count > 0
            || (requireCompleteMap && header.Masters.Count > 0);
        if (!required)
        {
            var ordinaryStyles = new Dictionary<ModKey, ResolvedMasterStyle>
            {
                [header.ModKey] = new(
                    header.ModKey,
                    MasterStyle.Full,
                    "ordinary-schema-v2",
                    null,
                    null,
                    false),
            };
            foreach (var master in header.Masters)
            {
                if (GameMasterStylePolicy.IsKnownFullMaster(gameId, master))
                {
                    ordinaryStyles[master] = new(
                        master,
                        MasterStyle.Full,
                        "game-profile:known-full",
                        null,
                        null,
                        null);
                }
            }
            return new(
                false,
                header,
                gameRelease,
                ordinaryStyles,
                null,
                null,
                string.Empty,
                header.Masters.All(ordinaryStyles.ContainsKey) ? false : null);
        }

        var candidates = new Dictionary<ModKey, List<StyleCandidate>>();
        var inputRelative = Relative(root, input);
        var inputSha256 = Sha256(input);
        AddCandidate(
            candidates,
            header.ModKey,
            header.IsSmall ? MasterStyle.Small : MasterStyle.Full,
            $"workspace-header:{inputRelative}",
            inputRelative,
            inputSha256,
            header.SmallFlagged);

        if (manifestEntries.Count > 0)
        {
            foreach (var entry in manifestEntries)
            {
                AddCandidate(
                    candidates,
                    entry.ModKey,
                    entry.Style,
                    $"manifest-header:{entry.RelativePath}",
                    entry.RelativePath,
                    entry.Sha256,
                    entry.SmallFlag);
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
                null,
                null);
        }
        foreach (var master in header.Masters.Where(master =>
                     GameMasterStylePolicy.IsKnownFullMaster(gameId, master)))
        {
            AddCandidate(
                candidates,
                master,
                MasterStyle.Full,
                "game-profile:known-full",
                null,
                null,
                null);
        }
        var unexpected = candidates.Keys.Where(owner => !expectedOwners.Contains(owner)).ToArray();
        if (unexpected.Length > 0)
        {
            throw Conflict(
                $"master-style manifest contains owners outside the plugin header: {string.Join(", ", unexpected)}");
        }

        var resolved = new Dictionary<ModKey, ResolvedMasterStyle>();
        foreach (var owner in expectedOwners)
        {
            if (!candidates.TryGetValue(owner, out var ownerCandidates) || ownerCandidates.Count == 0)
            {
                if (owner == header.ModKey
                    || (targetOwners?.Contains(owner) ?? requireCompleteMap))
                {
                    throw Unknown(
                        $"cannot confirm master style for {owner}; provide a workspace-local plugin header or hash-bound master-style manifest");
                }
                continue;
            }
            var styles = ownerCandidates.Select(static item => item.Style).Distinct().ToArray();
            if (styles.Length != 1)
            {
                var evidence = string.Join(", ", ownerCandidates.Select(static item =>
                    $"{StyleName(item.Style)} from {item.Source}"));
                throw Conflict($"conflicting master style evidence for {owner}: {evidence}");
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
                    inspected?.Sha256,
                    inspected?.SmallFlag));
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
        bool? referencesLightMaster;
        if (header.Masters.Any(master =>
                resolved.TryGetValue(master, out var style)
                && style.Style == MasterStyle.Small))
        {
            referencesLightMaster = true;
        }
        else if (header.Masters.Any(master => !resolved.ContainsKey(master)))
        {
            referencesLightMaster = null;
        }
        else
        {
            referencesLightMaster = false;
        }
        var package = SeparatedMasterPackage.Factory(
            gameRelease,
            header.ModKey,
            current.Style,
            references,
            lookup);
        CreateDirectoryWithoutReparsePoints(
            root,
            Path.GetDirectoryName(contextPath)!,
            "master-style context directory");
        if (File.Exists(contextPath) || Directory.Exists(contextPath))
        {
            EnsureNoReparsePoints(root, contextPath, "master-style context", Conflict);
            if (Directory.Exists(contextPath))
            {
                throw Conflict($"master-style context path is a directory: {contextPath}");
            }
        }
        WriteContext(contextPath, gameId, inputRelative, current, header.Masters, resolved);
        return new(
            true,
            header,
            gameRelease,
            resolved,
            lookup,
            package,
            contextPath,
            referencesLightMaster);
    }

    private static HashSet<ModKey> ResolveTargetOwners(
        PluginHeaderMetadata header,
        IEnumerable<uint> rawFormIds)
    {
        var owners = new HashSet<ModKey>();
        foreach (var raw in rawFormIds)
        {
            var masterIndex = (int)(raw >> 24);
            if (masterIndex < header.Masters.Count)
            {
                owners.Add(header.Masters[masterIndex]);
            }
            else if (masterIndex == header.Masters.Count)
            {
                owners.Add(header.ModKey);
            }
        }
        return owners;
    }

    public bool TryGetStyle(FormKey formKey, out ResolvedMasterStyle style) =>
        TryGetStyle(formKey.ModKey, out style);

    public bool TryGetStyle(ModKey modKey, out ResolvedMasterStyle style) =>
        _styles.TryGetValue(modKey, out style!);

    internal static string ContextPathFor(string root, string input, ModKey modKey)
    {
        var relative = Relative(root, input).ToLowerInvariant();
        var identity = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(relative)))[..16].ToLowerInvariant();
        return Path.Combine(
            root,
            "work",
            "plugin_context",
            "resolved",
            identity,
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
                throw Conflict("master-style manifest must be a JSON file");
            }
            if (!File.Exists(explicitPath))
            {
                throw Stale($"master-style manifest does not exist: {explicitPath}");
            }
            EnsureRegularSingleLinkFile(root, explicitPath, "master-style manifest", Stale);
            return explicitPath;
        }

        var defaultPath = contextPath.Replace(
            ".resolved-master-styles.json",
            ".master-styles.json",
            StringComparison.Ordinal);
        if (!File.Exists(defaultPath)) return null;
        EnsureRegularSingleLinkFile(root, defaultPath, "default master-style manifest", Stale);
        return defaultPath;
    }

    private static MasterStyleManifest ReadManifest(
        string path,
        string gameId,
        ModKey plugin)
    {
        MasterStyleManifest manifest;
        try
        {
            var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
            manifest = JsonSerializer.Deserialize<MasterStyleManifest>(File.ReadAllText(path, StrictUtf8), options)
                ?? throw Conflict("master-style manifest is empty");
        }
        catch (JsonException exception)
        {
            throw Conflict($"master-style manifest is invalid JSON: {exception.Message}");
        }
        catch (DecoderFallbackException exception)
        {
            throw Conflict($"master-style manifest is not valid UTF-8: {exception.Message}");
        }
        catch (IOException exception)
        {
            throw Stale($"master-style manifest could not be read: {exception.Message}");
        }
        catch (UnauthorizedAccessException exception)
        {
            throw Stale($"master-style manifest could not be read: {exception.Message}");
        }
        if (manifest.SchemaVersion != 2)
        {
            throw Conflict(
                $"unsupported master-style manifest schema_version={manifest.SchemaVersion}");
        }
        if (!string.Equals(manifest.GameId, gameId, StringComparison.Ordinal))
        {
            throw Conflict(
                $"master-style manifest game_id {manifest.GameId} does not match {gameId}");
        }
        if (!string.Equals(manifest.Plugin, plugin.FileName.String, StringComparison.OrdinalIgnoreCase))
        {
            throw Conflict(
                $"master-style manifest plugin {manifest.Plugin} does not match {plugin}");
        }
        if (manifest.Masters is not { Count: > 0 })
        {
            throw Conflict("master-style manifest masters must not be empty");
        }
        return manifest;
    }

    private static ManifestStyleEvidence ReadManifestEvidence(
        string root,
        ModKey expectedModKey,
        MasterStyleManifestEntry entry)
    {
        var relativePath = (entry.InspectedPath ?? string.Empty).Trim();
        if (string.IsNullOrWhiteSpace(relativePath)
            || relativePath.Contains('\\')
            || Path.IsPathFullyQualified(relativePath)
            || relativePath.Split('/').Any(static part =>
                string.IsNullOrWhiteSpace(part) || part is "." or ".."))
        {
            throw Stale(
                $"manifest inspected_path is not a canonical workspace-relative path for {expectedModKey}");
        }

        var inspectedPath = Path.GetFullPath(Path.Combine(
            root,
            relativePath.Replace('/', Path.DirectorySeparatorChar)));
        EnsureInside(root, inspectedPath, $"manifest inspected master {expectedModKey}");
        EnsureRegularSingleLinkFile(
            root,
            inspectedPath,
            $"manifest inspected master {expectedModKey}",
            Stale);
        if (!File.Exists(inspectedPath))
        {
            throw Stale(
                $"manifest inspected master does not exist for {expectedModKey}: {relativePath}");
        }
        var inspectedSha256 = (entry.InspectedSha256 ?? string.Empty).Trim();
        if (!IsSha256(inspectedSha256))
        {
            throw Stale($"manifest inspected_sha256 is invalid for {expectedModKey}");
        }

        var actualSha256 = Sha256Evidence(
            inspectedPath,
            $"manifest inspected master {expectedModKey}");
        if (!string.Equals(actualSha256, inspectedSha256, StringComparison.OrdinalIgnoreCase))
        {
            throw Stale(
                $"manifest inspected_sha256 is stale for {expectedModKey}: expected {actualSha256}, found {inspectedSha256}");
        }
        if (entry.SmallFlag is null)
        {
            throw Stale($"manifest small_flag is missing for {expectedModKey}");
        }

        var header = ReadEvidenceHeader(
            inspectedPath,
            $"manifest inspected master {expectedModKey}");
        if (header.ModKey != expectedModKey)
        {
            throw Conflict(
                $"manifest inspected master identity mismatch: expected {expectedModKey}, found {header.ModKey}");
        }
        if (header.SmallFlagged != entry.SmallFlag.Value)
        {
            throw Conflict(
                $"manifest small_flag conflicts with the inspected header for {expectedModKey}");
        }

        var manifestStyle = ParseStyle(entry.MasterStyle);
        var inspectedStyle = header.IsSmall ? MasterStyle.Small : MasterStyle.Full;
        if (manifestStyle != inspectedStyle)
        {
            throw Conflict(
                $"manifest master_style {StyleName(manifestStyle)} conflicts with inspected style {StyleName(inspectedStyle)} for {expectedModKey}");
        }
        return new(
            expectedModKey,
            manifestStyle,
            Relative(root, inspectedPath),
            actualSha256,
            header.SmallFlagged);
    }

    private static MasterStyle ParseStyle(string? value) => (value ?? string.Empty).Trim().ToLowerInvariant() switch
    {
        "full" => MasterStyle.Full,
        "light" or "small" => MasterStyle.Small,
        _ => throw Conflict($"unsupported master_style: {value}"),
    };

    private static void AddCandidate(
        IDictionary<ModKey, List<StyleCandidate>> candidates,
        ModKey modKey,
        MasterStyle style,
        string source,
        string? relativePath,
        string? sha256,
        bool? smallFlag)
    {
        if (!candidates.TryGetValue(modKey, out var values))
        {
            values = [];
            candidates.Add(modKey, values);
        }
        values.Add(new(style, source, relativePath, sha256, smallFlag));
    }

    private static void WriteContext(
        string contextPath,
        string gameId,
        string inputRelative,
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
            input_sha256 = current.Sha256,
            current_style = StyleName(current.Style),
            current_evidence_source = current.EvidenceSource,
            current_inspected_path = current.RelativePath?.Replace('\\', '/'),
            current_inspected_sha256 = current.Sha256,
            current_small_flag = current.SmallFlag,
            masters = masters.Select(master =>
            {
                if (!resolved.TryGetValue(master, out var item))
                {
                    return new
                    {
                        mod_key = master.FileName.String,
                        master_style = "unknown",
                        evidence_source = "unresolved:unseparated-master-order",
                        inspected_path = (string?)null,
                        inspected_sha256 = (string?)null,
                        small_flag = (bool?)null,
                    };
                }
                return new
                {
                    mod_key = master.FileName.String,
                    master_style = StyleName(item.Style),
                    evidence_source = item.EvidenceSource,
                    inspected_path = item.RelativePath?.Replace('\\', '/'),
                    inspected_sha256 = item.Sha256,
                    small_flag = item.SmallFlag,
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

    private static string Sha256(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    private static string Sha256Evidence(string path, string label)
    {
        try
        {
            return Sha256(path);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            throw Stale($"{label} could not be hashed: {exception.Message}");
        }
    }

    private static PluginHeaderMetadata ReadEvidenceHeader(string path, string label)
    {
        try
        {
            return PluginHeaderMetadata.Read(path);
        }
        catch (InvalidDataException exception)
        {
            throw Conflict($"{label} has an invalid TES4 header: {exception.Message}");
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            throw Stale($"{label} could not be read: {exception.Message}");
        }
    }

    private static bool IsSha256(string? value) =>
        value is not null
        && value.Length == 64
        && value.All(static character =>
            character is >= '0' and <= '9'
                or >= 'a' and <= 'f'
                or >= 'A' and <= 'F');

    private static InvalidDataException Unknown(string message) =>
        new($"master_style_unknown: {message}");

    private static InvalidDataException Stale(string message) =>
        new($"master_style_evidence_stale: {message}");

    private static InvalidDataException Conflict(string message) =>
        new($"master_style_conflict: {message}");

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

    private static void EnsureNoReparsePoints(
        string root,
        string path,
        string label,
        Func<string, InvalidDataException> error)
    {
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var pathFull = Path.GetFullPath(path);
        EnsureInside(rootFull, pathFull, label);
        var relative = Path.GetRelativePath(rootFull, pathFull);
        var current = rootFull;
        if (File.Exists(current) || Directory.Exists(current))
        {
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw error($"{label} traverses a reparse point: {current}");
            }
        }
        foreach (var part in relative.Split(
                     [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
                     StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, part);
            if (!File.Exists(current) && !Directory.Exists(current)) break;
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw error($"{label} traverses a reparse point: {current}");
            }
        }
    }

    private static void EnsureRegularSingleLinkFile(
        string root,
        string path,
        string label,
        Func<string, InvalidDataException> error)
    {
        EnsureNoReparsePoints(root, path, label, error);
        if (!File.Exists(path) || Directory.Exists(path))
        {
            throw error($"{label} is not a regular file: {path}");
        }
        if (!OperatingSystem.IsWindows())
        {
            throw error($"{label} requires Windows file identity validation: {path}");
        }

        try
        {
            using SafeFileHandle handle = File.OpenHandle(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                FileOptions.None);
            if (!GetFileInformationByHandle(handle, out var information))
            {
                throw new IOException(
                    $"could not read file identity for {path}",
                    Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
            }
            if (information.NumberOfLinks != 1)
            {
                throw error($"{label} has multiple hardlinks: {path}");
            }
        }
        catch (InvalidDataException)
        {
            throw;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            throw error($"{label} file identity could not be validated: {exception.Message}");
        }
    }

    private static void CreateDirectoryWithoutReparsePoints(
        string root,
        string path,
        string label)
    {
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var pathFull = Path.GetFullPath(path);
        EnsureInside(rootFull, pathFull, label);
        var current = rootFull;
        foreach (var part in Path.GetRelativePath(rootFull, pathFull).Split(
                     [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
                     StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, part);
            if (File.Exists(current) && !Directory.Exists(current))
            {
                throw Conflict($"{label} contains a file where a directory is required: {current}");
            }
            Directory.CreateDirectory(current);
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw Conflict($"{label} traverses a reparse point: {current}");
            }
        }
    }

    private sealed record StyleCandidate(
        MasterStyle Style,
        string Source,
        string? RelativePath,
        string? Sha256,
        bool? SmallFlag);

    private sealed record ManifestStyleEvidence(
        ModKey ModKey,
        MasterStyle Style,
        string RelativePath,
        string Sha256,
        bool SmallFlag);

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
        public string? ModKey { get; set; } = string.Empty;

        [JsonPropertyName("master_style")]
        public string? MasterStyle { get; set; } = string.Empty;

        [JsonPropertyName("inspected_path")]
        public string? InspectedPath { get; set; } = string.Empty;

        [JsonPropertyName("inspected_sha256")]
        public string? InspectedSha256 { get; set; } = string.Empty;

        [JsonPropertyName("small_flag")]
        public bool? SmallFlag { get; set; }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out ByHandleFileInformation information);

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public uint CreationTimeLow;
        public uint CreationTimeHigh;
        public uint LastAccessTimeLow;
        public uint LastAccessTimeHigh;
        public uint LastWriteTimeLow;
        public uint LastWriteTimeHigh;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }
}
