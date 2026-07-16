using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

internal sealed class Program
{
    private const string AdapterId = "mutagen-bethesda-plugin";
    private static readonly HashSet<string> SupportedCapabilityLevels =
        new(StringComparer.Ordinal)
        {
            "read_only",
            "experimental_write",
            "stable",
        };
    private static readonly HashSet<string> PluginExtensions =
        new(StringComparer.OrdinalIgnoreCase) { ".esp", ".esm", ".esl" };
    private static readonly HashSet<string> JsonlExtensions =
        new(StringComparer.OrdinalIgnoreCase) { ".jsonl" };
    private static readonly HashSet<string> MarkdownExtensions =
        new(StringComparer.OrdinalIgnoreCase) { ".md" };
    private static readonly string[] RiskyPathMarkers =
    [
        "SteamLibrary",
        "steamapps",
        "Skyrim Special Edition\\Data",
        "Skyrim Special Edition/Data",
        "Fallout 4\\Data",
        "Fallout 4/Data",
        "ModOrganizer",
        "Vortex",
        "AppData",
        "Documents\\My Games",
    ];

    public static int Main(string[] args)
    {
        Options? options = null;
        try
        {
            options = Options.Parse(args);
            if (options.Command is not ("apply" or "verify" or "export"))
            {
                Console.Error.WriteLine(
                    "Usage: SkyrimPluginTextTool apply|verify|export --game <identity> "
                    + "--mutagen-release <release> --capability-level <level> "
                    + "--project-root <path> --input-plugin <path> "
                    + "[--translation-jsonl <path> --output-plugin <path> | "
                    + "--output-jsonl <path>] --report <path> [--dry-run]");
                return 2;
            }

            var game = Require(options.Game, "--game");
            var mutagenRelease = Require(options.MutagenRelease, "--mutagen-release");
            var capabilityLevel = Require(options.CapabilityLevel, "--capability-level");
            if (!SupportedCapabilityLevels.Contains(capabilityLevel))
            {
                throw new ArgumentException($"Unsupported capability level: {capabilityLevel}");
            }
            var adapter = PluginAdapterRegistry.ResolveForIdentity(mutagenRelease, game);
            return options.Command == "export"
                ? Export(options, adapter, game, capabilityLevel)
                : ApplyOrVerify(options, adapter, game, capabilityLevel);
        }
        catch (Exception ex)
        {
            CleanupFailedApply(options);
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static void CleanupFailedApply(Options? options)
    {
        if (options?.Command != "apply" || string.IsNullOrWhiteSpace(options.OutputPlugin))
        {
            return;
        }
        try
        {
            var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
            var outputPlugin = FullPath(options.OutputPlugin);
            ValidateRolePath(
                projectRoot,
                outputPlugin,
                "output plugin",
                ["out"],
                PluginExtensions);
            EnsureNoCleanupAlias(options, outputPlugin);
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
        }
        catch
        {
            // Invalid or unsafe output paths are never cleanup targets.
        }
    }

    private static int ApplyOrVerify(
        Options options,
        IPluginTextAdapter adapter,
        string game,
        string capabilityLevel)
    {
        if (options.Command == "apply"
            && capabilityLevel is not ("experimental_write" or "stable"))
        {
            throw new ArgumentException(
                $"Capability level {capabilityLevel} does not permit plugin apply.");
        }
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPlugin = FullPath(Require(options.InputPlugin, "--input-plugin"));
        var translationJsonl = FullPath(Require(options.TranslationJsonl, "--translation-jsonl"));
        var outputPlugin = FullPath(Require(options.OutputPlugin, "--output-plugin"));
        var reportPath = FullPath(Require(options.Report, "--report"));
        var isApply = options.Command == "apply";
        ValidateApplyVerifyPaths(
            projectRoot,
            inputPlugin,
            translationJsonl,
            outputPlugin,
            reportPath);
        if (isApply)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(outputPlugin)!);
            AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
        }

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

            var rows = ReadRows(translationJsonl)
                .Where(static row => string.Equals(
                    row.Risk,
                    "candidate",
                    StringComparison.OrdinalIgnoreCase))
                .Where(static row => !string.IsNullOrWhiteSpace(row.Target))
                .ToList();
            var request = new PluginTextRequest(
                game,
                capabilityLevel,
                inputPlugin,
                outputPlugin,
                options.DryRun);

            AdapterResult result;
            if (isApply)
            {
                AtomicPluginOutput.PrepareTarget(outputPlugin);
                result = adapter.Apply(request, rows);
            }
            else
            {
                result = adapter.Verify(request, rows);
            }

            var exitCode = result.Missing.Count > 0
                || result.Unsupported.Count > 0
                || (!isApply && !result.StructuralValidationSucceeded)
                ? 2
                : 0;
            var status = exitCode == 0 ? "ready" : "blocked";
            WriteReport(
                reportPath,
                projectRoot,
                inputPlugin,
                translationJsonl,
                outputPlugin,
                options.DryRun,
                rows.Count,
                result,
                game,
                capabilityLevel,
                options.Command,
                status);
            Console.WriteLine($"Mutagen plugin text report: {reportPath}");
            Console.WriteLine($"Applied rows: {result.Applied.Count} / {rows.Count}");
            if (isApply && exitCode != 0)
            {
                AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            }
            return exitCode;
        }
        catch
        {
            if (isApply)
            {
                AtomicPluginOutput.CleanupFailure(string.Empty, outputPlugin);
            }
            throw;
        }
    }

    private static int Export(
        Options options,
        IPluginTextAdapter adapter,
        string game,
        string capabilityLevel)
    {
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPlugin = FullPath(Require(options.InputPlugin, "--input-plugin"));
        var outputJsonl = FullPath(Require(options.OutputJsonl, "--output-jsonl"));
        var reportPath = FullPath(Require(options.Report, "--report"));
        ValidateExportPaths(projectRoot, inputPlugin, outputJsonl, reportPath);
        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

        try
        {
            var result = adapter.Export(
                new PluginExportRequest(
                    game,
                    capabilityLevel,
                    inputPlugin,
                    Relative(projectRoot, inputPlugin),
                    outputJsonl));
            var reason = result.Reason;
            if (result.Blocked)
            {
                reason = AppendCleanupFailure(
                    reason,
                    TryCleanupExportOutput(outputJsonl));
            }
            WriteExportReport(
                reportPath,
                projectRoot,
                inputPlugin,
                outputJsonl,
                result.RowCount,
                result.Blocked ? "blocked" : "ready",
                reason,
                game,
                capabilityLevel,
                result.Coverage,
                result.Traits);
            Console.WriteLine($"Mutagen plugin export report: {reportPath}");
            Console.WriteLine($"Exported rows: {result.RowCount}");
            if (result.Blocked)
            {
                Console.Error.WriteLine($"Mutagen plugin export failed: {reason}");
                return 2;
            }
            return 0;
        }
        catch (Exception exc)
        {
            var reason = AppendCleanupFailure(
                exc.Message,
                TryCleanupExportOutput(outputJsonl));
            WriteExportReport(
                reportPath,
                projectRoot,
                inputPlugin,
                outputJsonl,
                0,
                "blocked",
                reason,
                game,
                capabilityLevel,
                string.Empty,
                PluginTraits.FromPath(inputPlugin));
            Console.Error.WriteLine($"Mutagen plugin export failed: {reason}");
            return 2;
        }
    }

    private static void ValidateApplyVerifyPaths(
        string projectRoot,
        string inputPlugin,
        string translationJsonl,
        string outputPlugin,
        string reportPath)
    {
        ValidateRolePath(
            projectRoot,
            inputPlugin,
            "input plugin",
            [Path.Combine("work", "extracted_mods")],
            PluginExtensions);
        ValidateRolePath(
            projectRoot,
            outputPlugin,
            "output plugin",
            ["out"],
            PluginExtensions);
        ValidateRolePath(
            projectRoot,
            translationJsonl,
            "translation jsonl",
            ["translated"],
            JsonlExtensions);
        ValidateRolePath(
            projectRoot,
            reportPath,
            "report",
            ["qa"],
            MarkdownExtensions);
        EnsureDistinctRoles(
            ("input plugin", inputPlugin),
            ("translation jsonl", translationJsonl),
            ("output plugin", outputPlugin),
            ("report", reportPath));
    }

    private static void ValidateExportPaths(
        string projectRoot,
        string inputPlugin,
        string outputJsonl,
        string reportPath)
    {
        ValidateRolePath(
            projectRoot,
            inputPlugin,
            "input plugin",
            [Path.Combine("work", "extracted_mods"), "out"],
            PluginExtensions);
        ValidateRolePath(
            projectRoot,
            outputJsonl,
            "output jsonl",
            ["source"],
            JsonlExtensions);
        ValidateRolePath(
            projectRoot,
            reportPath,
            "report",
            ["qa"],
            MarkdownExtensions);
        EnsureDistinctRoles(
            ("input plugin", inputPlugin),
            ("output jsonl", outputJsonl),
            ("report", reportPath));
    }

    private static void ValidateRolePath(
        string projectRoot,
        string path,
        string label,
        IEnumerable<string> allowedRoots,
        IReadOnlySet<string> allowedExtensions)
    {
        EnsureInside(path, projectRoot, label);
        EnsureNoRiskyMarker(path);
        if (!allowedExtensions.Contains(Path.GetExtension(path)))
        {
            throw new InvalidOperationException(
                $"{label} has an unsupported extension: {path}");
        }
        var allowed = allowedRoots.Any(relativeRoot =>
            IsInside(path, FullPath(Path.Combine(projectRoot, relativeRoot))));
        if (!allowed)
        {
            throw new InvalidOperationException(
                $"{label} is outside its allowed role directory: {path}");
        }
        RejectReparsePoints(projectRoot, path, label);
    }

    private static void EnsureDistinctRoles(params (string Label, string Path)[] roles)
    {
        for (var left = 0; left < roles.Length; left++)
        {
            for (var right = left + 1; right < roles.Length; right++)
            {
                if (string.Equals(
                    FullPath(roles[left].Path),
                    FullPath(roles[right].Path),
                    StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException(
                        $"{roles[left].Label} and {roles[right].Label} must be different paths.");
                }
            }
        }
    }

    private static void EnsureNoCleanupAlias(Options options, string outputPlugin)
    {
        foreach (var (label, path) in new[]
                 {
                     ("input plugin", options.InputPlugin),
                     ("translation jsonl", options.TranslationJsonl),
                     ("report", options.Report),
                 })
        {
            if (!string.IsNullOrWhiteSpace(path)
                && string.Equals(
                    FullPath(path),
                    outputPlugin,
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    $"output plugin and {label} must be different paths.");
            }
        }
    }

    private static void RejectReparsePoints(
        string projectRoot,
        string path,
        string label)
    {
        var root = FullPath(projectRoot);
        var target = FullPath(path);
        var relative = Path.GetRelativePath(root, target);
        var current = root;
        CheckReparsePoint(current, label);
        foreach (var component in relative.Split(
                     [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
                     StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, component);
            try
            {
                CheckReparsePoint(current, label);
            }
            catch (FileNotFoundException)
            {
                break;
            }
            catch (DirectoryNotFoundException)
            {
                break;
            }
        }
    }

    private static void CheckReparsePoint(string path, string label)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidOperationException(
                $"{label} contains a reparse point: {path}");
        }
    }

    private static List<TranslationRow> ReadRows(string translationJsonl)
    {
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        var rows = new List<TranslationRow>();
        foreach (var line in File.ReadLines(translationJsonl, Encoding.UTF8))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            var row = JsonSerializer.Deserialize<TranslationRow>(line, options);
            if (row is not null) rows.Add(row);
        }
        return rows;
    }

    private static void WriteReport(
        string reportPath,
        string projectRoot,
        string inputPlugin,
        string translationJsonl,
        string outputPlugin,
        bool dryRun,
        int candidateCount,
        AdapterResult result,
        string game,
        string capabilityLevel,
        string operation,
        string status)
    {
        var lines = new List<string>
        {
            "# Mutagen Plugin Text Tool Report",
            "",
            $"- game_id: {game}",
            "- game_profile_version: 2",
            $"- plugin_adapter: {AdapterId}",
            "- plugin_adapter_version: 1",
            $"- support_level: {SupportLabel(capabilityLevel)}",
            $"- plugin_text_capability_level: {capabilityLevel}",
            $"- Operation: {operation}",
            $"- Status: {status}",
            $"- localized: {ReportTrait(result.Traits.Localized)}",
            $"- light_by_extension: {ReportTrait(result.Traits.LightByExtension)}",
            $"- light_by_header: {ReportTrait(result.Traits.LightByHeader)}",
            $"- contains_unsupported_light_formids: {ReportTrait(result.Traits.ContainsUnsupportedLightFormIds)}",
            $"- Input plugin: {Relative(projectRoot, inputPlugin)}",
            $"- Input SHA256: {Sha256OrEmpty(inputPlugin)}",
            $"- Translation JSONL: {Relative(projectRoot, translationJsonl)}",
            $"- Translation SHA256: {Sha256OrEmpty(translationJsonl)}",
            $"- Output plugin: {Relative(projectRoot, outputPlugin)}",
            $"- Output SHA256: {Sha256OrEmpty(outputPlugin)}",
            $"- Dry run: {dryRun}",
            $"- Candidate rows: {candidateCount}",
            $"- Applied rows: {result.Applied.Count}",
            $"- Missing rows: {result.Missing.Count}",
            $"- Unsupported rows: {result.Unsupported.Count}",
            $"- Reparse succeeded: {result.ReparseSucceeded}",
            $"- Reparse target: {result.ReparseTarget}",
            $"- Structural validation target: {result.ReparseTarget}",
            $"- Input record count: {result.InputRecordCount}",
            $"- Output record count: {result.OutputRecordCount}",
            $"- Record count preserved: {result.RecordCountPreserved}",
            $"- Input FormKeys: {ReportList(result.InputFormKeys)}",
            $"- Output FormKeys: {ReportList(result.OutputFormKeys)}",
            $"- FormKey set preserved: {result.FormKeySetPreserved}",
            $"- Input masters: {ReportList(result.InputMasters)}",
            $"- Output masters: {ReportList(result.OutputMasters)}",
            $"- Masters preserved: {result.MastersPreserved}",
            $"- Parsed structural and payload invariant verified: {result.BinaryInvariantVerified}",
            $"- Parsed structural and payload invariant records checked: {result.BinaryInvariantRecordsChecked}",
            $"- Parsed structural and payload invariant targets verified: {result.BinaryInvariantTargetsVerified}",
            $"- Allowed header changes: {ReportList(result.AllowedHeaderChanges)}",
            $"- Structural validation succeeded: {result.StructuralValidationSucceeded}",
            "",
            "## Applied",
            "",
        };
        lines.AddRange(result.Applied.Count == 0
            ? ["No applied rows."]
            : result.Applied.Select(item => $"- {item}"));
        AppendSection(lines, "Missing", result.Missing, "No missing rows.");
        AppendSection(lines, "Unsupported", result.Unsupported, "No unsupported rows.");
        AppendSection(
            lines,
            "Parsed Structural and Payload Invariant Issues",
            result.BinaryInvariantIssues,
            "No parsed structural or payload invariant issues.");
        AppendSection(lines, "Notes", result.Skipped, "No notes.");
        lines.Add("");
        lines.Add("## Safety");
        lines.Add("");
        lines.Add("- All paths were checked to be inside the project root.");
        lines.Add("- This tool does not read real game, Steam, MO2/Vortex, AppData, or Documents/My Games paths.");
        if (capabilityLevel == "experimental_write")
        {
            lines.Add("- Experimental plugin writeback requires independent in-game validation.");
        }
        lines.Add("- This tool writes only to the requested project-local output path.");
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static void AppendSection(
        List<string> lines,
        string heading,
        IEnumerable<string> values,
        string emptyMessage)
    {
        var items = values.ToArray();
        lines.Add("");
        lines.Add($"## {heading}");
        lines.Add("");
        lines.AddRange(items.Length == 0 ? [emptyMessage] : items.Select(item => $"- {item}"));
    }

    private static void WriteExportReport(
        string reportPath,
        string projectRoot,
        string inputPlugin,
        string outputJsonl,
        int rowCount,
        string status,
        string reason,
        string game,
        string capabilityLevel,
        string coverage,
        PluginTraits traits)
    {
        var outputHash = string.Equals(status, "ready", StringComparison.Ordinal)
            ? Sha256OrEmpty(outputJsonl)
            : Sha256OrUnavailable(outputJsonl);
        var lines = new List<string>
        {
            "# Mutagen Plugin Text Tool Report",
            "",
            $"- game_id: {game}",
            "- game_profile_version: 2",
            $"- plugin_adapter: {AdapterId}",
            "- plugin_adapter_version: 1",
            $"- support_level: {SupportLabel(capabilityLevel)}",
            $"- plugin_text_capability_level: {capabilityLevel}",
            "- Operation: export",
            $"- localized: {ReportTrait(traits.Localized)}",
            $"- light_by_extension: {ReportTrait(traits.LightByExtension)}",
            $"- light_by_header: {ReportTrait(traits.LightByHeader)}",
            $"- contains_unsupported_light_formids: {ReportTrait(traits.ContainsUnsupportedLightFormIds)}",
            $"- Status: {status}",
            $"- Input plugin: {Relative(projectRoot, inputPlugin)}",
            $"- Input SHA256: {Sha256OrEmpty(inputPlugin)}",
            $"- Output JSONL: {Relative(projectRoot, outputJsonl)}",
            $"- Output JSONL SHA256: {outputHash}",
            $"- Exported rows: {rowCount}",
        };
        if (!string.IsNullOrWhiteSpace(coverage)) lines.Add($"- Export coverage: {coverage}");
        if (!string.IsNullOrWhiteSpace(reason))
        {
            lines.Add($"- Reason: {reason.Replace('\r', ' ').Replace('\n', ' ')}");
        }
        lines.Add("");
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static string SupportLabel(string capabilityLevel) => capabilityLevel switch
    {
        "stable" => "stable",
        "experimental_write" => "experimental",
        "read_only" => "read_only",
        _ => throw new ArgumentException($"Unsupported capability level: {capabilityLevel}"),
    };

    private static string ReportTrait(bool? value) => value switch
    {
        true => "true",
        false => "false",
        null => "unknown",
    };

    private static string TryCleanupExportOutput(string outputJsonl)
    {
        try
        {
            AtomicPluginOutput.CleanupFailure(string.Empty, outputJsonl);
            return string.Empty;
        }
        catch (Exception exc)
        {
            return exc.Message;
        }
    }

    private static string AppendCleanupFailure(string reason, string cleanupFailure) =>
        string.IsNullOrWhiteSpace(cleanupFailure)
            ? reason
            : $"{reason}; output cleanup failed: {cleanupFailure}";

    private static string Require(string? value, string name)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException($"Missing required argument: {name}");
        }
        return value;
    }

    private static string FullPath(string path) =>
        Path.GetFullPath(path).TrimEnd(
            Path.DirectorySeparatorChar,
            Path.AltDirectorySeparatorChar);

    private static void EnsureInside(string child, string parent, string label)
    {
        if (!IsInside(child, parent))
        {
            throw new InvalidOperationException(
                $"{label} is outside project root: {FullPath(child)}");
        }
    }

    private static bool IsInside(string child, string parent)
    {
        var childFull = FullPath(child);
        var parentFull = FullPath(parent);
        return string.Equals(childFull, parentFull, StringComparison.OrdinalIgnoreCase)
            || childFull.StartsWith(
                parentFull + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase);
    }

    private static void EnsureNoRiskyMarker(string path)
    {
        foreach (var marker in RiskyPathMarkers)
        {
            if (path.Contains(marker, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException($"Refusing risky path marker {marker}: {path}");
            }
        }
    }

    private static string Relative(string root, string path) =>
        Path.GetRelativePath(root, path).Replace('\\', '/');

    private static string Sha256OrEmpty(string path)
    {
        if (!File.Exists(path)) return string.Empty;
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    private static string Sha256OrUnavailable(string path)
    {
        try
        {
            return Sha256OrEmpty(path);
        }
        catch (IOException)
        {
            return "unavailable";
        }
        catch (UnauthorizedAccessException)
        {
            return "unavailable";
        }
    }

    private static string ReportList(IEnumerable<string> values)
    {
        var items = values.ToArray();
        return items.Length == 0 ? "<none>" : string.Join("; ", items);
    }

    private sealed class Options
    {
        public string Command { get; private set; } = "";
        public string? Game { get; private set; }
        public string? MutagenRelease { get; private set; }
        public string? CapabilityLevel { get; private set; }
        public string? ProjectRoot { get; private set; }
        public string? InputPlugin { get; private set; }
        public string? TranslationJsonl { get; private set; }
        public string? OutputPlugin { get; private set; }
        public string? OutputJsonl { get; private set; }
        public string? Report { get; private set; }
        public bool DryRun { get; private set; }

        public static Options Parse(string[] args)
        {
            var options = new Options();
            if (args.Length > 0) options.Command = args[0];
            for (var index = 1; index < args.Length; index++)
            {
                var arg = args[index];
                switch (arg)
                {
                    case "--game":
                        options.Game = Next(args, ref index, arg);
                        break;
                    case "--mutagen-release":
                        options.MutagenRelease = Next(args, ref index, arg);
                        break;
                    case "--capability-level":
                        options.CapabilityLevel = Next(args, ref index, arg);
                        break;
                    case "--project-root":
                        options.ProjectRoot = Next(args, ref index, arg);
                        break;
                    case "--input-plugin":
                        options.InputPlugin = Next(args, ref index, arg);
                        break;
                    case "--translation-jsonl":
                        options.TranslationJsonl = Next(args, ref index, arg);
                        break;
                    case "--output-plugin":
                        options.OutputPlugin = Next(args, ref index, arg);
                        break;
                    case "--output-jsonl":
                        options.OutputJsonl = Next(args, ref index, arg);
                        break;
                    case "--report":
                        options.Report = Next(args, ref index, arg);
                        break;
                    case "--dry-run":
                        options.DryRun = true;
                        break;
                    default:
                        throw new ArgumentException($"Unknown argument: {arg}");
                }
            }
            return options;
        }

        private static string Next(string[] args, ref int index, string name)
        {
            index++;
            if (index >= args.Length)
            {
                throw new ArgumentException($"Missing value for {name}");
            }
            return args[index];
        }
    }
}
