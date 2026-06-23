using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda.Plugins.Binary.Parameters;
using Mutagen.Bethesda.Skyrim;

internal sealed class Program
{
    private static readonly string[] RiskyPathMarkers =
    [
        "SteamLibrary",
        "steamapps",
        "Skyrim Special Edition\\Data",
        "ModOrganizer",
        "Vortex",
        "AppData",
        "Documents\\My Games",
    ];

    public static int Main(string[] args)
    {
        try
        {
            var options = Options.Parse(args);
            if (options.Command is not "apply")
            {
                Console.Error.WriteLine("Usage: SkyrimPluginTextTool apply --project-root <path> --input-plugin <path> --translation-jsonl <path> --output-plugin <path> --report <path> [--dry-run]");
                return 2;
            }

            return Apply(options);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static int Apply(Options options)
    {
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPlugin = FullPath(Require(options.InputPlugin, "--input-plugin"));
        var translationJsonl = FullPath(Require(options.TranslationJsonl, "--translation-jsonl"));
        var outputPlugin = FullPath(Require(options.OutputPlugin, "--output-plugin"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureInside(inputPlugin, projectRoot, "input plugin");
        EnsureInside(translationJsonl, projectRoot, "translation jsonl");
        EnsureInside(outputPlugin, projectRoot, "output plugin");
        EnsureInside(reportPath, projectRoot, "report");
        EnsureNoRiskyMarker(inputPlugin);
        EnsureNoRiskyMarker(translationJsonl);
        EnsureNoRiskyMarker(outputPlugin);
        EnsureNoRiskyMarker(reportPath);

        Directory.CreateDirectory(Path.GetDirectoryName(outputPlugin)!);
        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

        var rows = ReadRows(translationJsonl);
        var candidateRows = rows
            .Where(static row => string.Equals(row.Risk, "candidate", StringComparison.OrdinalIgnoreCase))
            .Where(static row => !string.IsNullOrWhiteSpace(row.Target))
            .ToList();

        var mod = SkyrimMod.CreateFromBinary(inputPlugin, SkyrimRelease.SkyrimSE);
        var buttonCursor = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        var dialogResponseIndexes = BuildDialogResponseIndexes(candidateRows);
        var applied = new List<string>();
        var skipped = new List<string>();
        var missing = new List<string>();
        var unsupported = new List<string>();

        foreach (var row in candidateRows)
        {
            switch (row.RecordType)
            {
                case "MGEF":
                    ApplyMagicEffect(mod, row, applied, missing, unsupported);
                    break;
                case "SPEL":
                    ApplySpell(mod, row, applied, missing, unsupported);
                    break;
                case "ARMO":
                    ApplyArmor(mod, row, applied, missing, unsupported);
                    break;
                case "WEAP":
                    ApplyWeapon(mod, row, applied, missing, unsupported);
                    break;
                case "CELL":
                    ApplyCell(mod, row, applied, missing, unsupported);
                    break;
                case "CLAS":
                    ApplyClass(mod, row, applied, missing, unsupported);
                    break;
                case "CLFM":
                    ApplyColorRecord(mod, row, applied, missing, unsupported);
                    break;
                case "PERK":
                    ApplyPerk(mod, row, applied, missing, unsupported);
                    break;
                case "FACT":
                    ApplyNamedRecord(mod.Factions, "Faction", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "ENCH":
                    ApplyNamedRecord(mod.ObjectEffects, "ObjectEffect", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "CONT":
                    ApplyNamedRecord(mod.Containers, "Container", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "MISC":
                    ApplyNamedRecord(mod.MiscItems, "MiscItem", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "ALCH":
                    ApplyIngestible(mod, row, applied, missing, unsupported);
                    break;
                case "WRLD":
                    ApplyNamedRecord(mod.Worldspaces, "Worldspace", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "DIAL":
                    ApplyNamedRecord(mod.DialogTopics, "DialogTopic", row, static item => item.EditorID, static (item, value) => item.Name = value, applied, missing, unsupported);
                    break;
                case "INFO":
                    ApplyDialogResponses(mod, row, dialogResponseIndexes, applied, missing, unsupported);
                    break;
                case "QUST":
                    ApplyQuest(mod, row, applied, missing, unsupported);
                    break;
                case "MESG":
                    ApplyMessage(mod, row, buttonCursor, applied, missing, unsupported);
                    break;
                default:
                    unsupported.Add(Describe(row, $"unsupported record type {row.RecordType}"));
                    break;
            }
        }

        if (!options.DryRun)
        {
            mod.BeginWrite
                .ToPath(outputPlugin)
                .WithLoadOrderFromHeaderMasters()
                .WithNoDataFolder()
                .NoModKeySync()
                .WithUtf8Encoding()
                .WithMastersListContent(MastersListContentOption.NoCheck)
                .Write();
        }
        else
        {
            skipped.Add("Dry run: plugin write skipped.");
        }

        WriteReport(reportPath, projectRoot, inputPlugin, translationJsonl, outputPlugin, options.DryRun, candidateRows.Count, applied, missing, unsupported, skipped);
        Console.WriteLine($"Mutagen plugin text report: {reportPath}");
        Console.WriteLine($"Applied rows: {applied.Count} / {candidateRows.Count}");
        if (missing.Count > 0 || unsupported.Count > 0)
        {
            return 2;
        }
        return 0;
    }

    private static void ApplyMagicEffect(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.MagicEffects.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "MagicEffect not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DNAM")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported MagicEffect subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplySpell(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Spells.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Spell not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Spell subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyArmor(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Armors.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Armor not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Armor subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyWeapon(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Weapons.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Weapon not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Weapon subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyCell(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = EnumerateCells(mod).FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Cell not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Cell subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyColorRecord(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Colors.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "ColorRecord not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported ColorRecord subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyClass(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Classes.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Class not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Class subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyPerk(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Perks.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Perk not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Perk subrecord {row.SubrecordType}"));
        }
    }

    private static IEnumerable<Cell> EnumerateCells(SkyrimMod mod)
    {
        foreach (var block in mod.Cells.Records)
        {
            foreach (var subBlock in block.SubBlocks)
            {
                foreach (var cell in subBlock.Cells)
                {
                    yield return cell;
                }
            }
        }
    }

    private static void ApplyNamedRecord<TRecord>(
        IEnumerable<TRecord> records,
        string recordLabel,
        TranslationRow row,
        Func<TRecord, string?> editorId,
        Action<TRecord, string> setName,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
        where TRecord : class
    {
        var record = records.FirstOrDefault(item => SameEditorId(editorId(item), row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, $"{recordLabel} not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            setName(record, row.Target);
            applied.Add(Describe(row, "Name"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported {recordLabel} subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyIngestible(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Ingestibles.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Ingestible not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Ingestible subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyQuest(SkyrimMod mod, TranslationRow row, List<string> applied, List<string> missing, List<string> unsupported)
    {
        var record = mod.Quests.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Quest not found"));
            return;
        }

        if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Quest subrecord {row.SubrecordType}"));
        }
    }

    private static void ApplyMessage(
        SkyrimMod mod,
        TranslationRow row,
        Dictionary<string, int> buttonCursor,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
    {
        var record = mod.Messages.FirstOrDefault(item => SameEditorId(item.EditorID, row.EditorId));
        if (record is null)
        {
            missing.Add(Describe(row, "Message not found"));
            return;
        }

        if (row.SubrecordType == "DESC")
        {
            record.Description = row.Target;
            applied.Add(Describe(row, "Description"));
        }
        else if (row.SubrecordType == "FULL")
        {
            record.Name = row.Target;
            applied.Add(Describe(row, "Name"));
        }
        else if (row.SubrecordType == "ITXT")
        {
            var key = $"{row.FormId}|{row.EditorId}";
            buttonCursor.TryGetValue(key, out var buttonIndex);
            if (buttonIndex >= record.MenuButtons.Count)
            {
                missing.Add(Describe(row, $"Message button index {buttonIndex} not found"));
                return;
            }

            record.MenuButtons[buttonIndex].Text = row.Target;
            buttonCursor[key] = buttonIndex + 1;
            applied.Add(Describe(row, $"MenuButtons[{buttonIndex}].Text"));
        }
        else
        {
            unsupported.Add(Describe(row, $"unsupported Message subrecord {row.SubrecordType}"));
        }
    }

    private static Dictionary<string, int> BuildDialogResponseIndexes(List<TranslationRow> candidateRows)
    {
        var indexes = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        var groups = candidateRows
            .Where(static row => row.RecordType == "INFO" && row.SubrecordType == "NAM1")
            .GroupBy(static row => NormalizeFormId(row.FormId));

        foreach (var group in groups)
        {
            var responseIndex = 0;
            foreach (var row in group.OrderBy(static row => row.SubrecordIndex))
            {
                indexes[DialogResponseIndexKey(row)] = responseIndex;
                responseIndex++;
            }
        }

        return indexes;
    }

    private static string DialogResponseIndexKey(TranslationRow row)
    {
        return $"{NormalizeFormId(row.FormId)}|{row.SubrecordIndex}";
    }

    private static void ApplyDialogResponses(
        SkyrimMod mod,
        TranslationRow row,
        Dictionary<string, int> dialogResponseIndexes,
        List<string> applied,
        List<string> missing,
        List<string> unsupported)
    {
        var responseRecord = mod.DialogTopics.Records
            .SelectMany(static topic => topic.Responses)
            .FirstOrDefault(item => SameFormId(row.FormId, item.FormKey.IDString()));
        if (responseRecord is null)
        {
            missing.Add(Describe(row, "DialogResponses not found"));
            return;
        }

        if (row.SubrecordType == "RNAM")
        {
            responseRecord.Prompt = row.Target;
            applied.Add(Describe(row, "Prompt"));
            return;
        }

        if (row.SubrecordType != "NAM1")
        {
            unsupported.Add(Describe(row, $"unsupported DialogResponses subrecord {row.SubrecordType}"));
            return;
        }

        if (responseRecord.Responses.Count == 0)
        {
            missing.Add(Describe(row, "DialogResponses has no response text entries"));
            return;
        }

        if (!dialogResponseIndexes.TryGetValue(DialogResponseIndexKey(row), out var responseIndex))
        {
            missing.Add(Describe(row, "Dialog response index not found"));
            return;
        }

        if (responseIndex >= responseRecord.Responses.Count)
        {
            missing.Add(Describe(row, $"Dialog response index {responseIndex} not found"));
            return;
        }

        responseRecord.Responses[responseIndex].Text = row.Target;
        applied.Add(Describe(row, $"Responses[{responseIndex}].Text"));
    }

    private static List<TranslationRow> ReadRows(string translationJsonl)
    {
        var options = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        };
        var rows = new List<TranslationRow>();
        foreach (var line in File.ReadLines(translationJsonl, Encoding.UTF8))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }
            var row = JsonSerializer.Deserialize<TranslationRow>(line, options);
            if (row is not null)
            {
                rows.Add(row);
            }
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
        List<string> applied,
        List<string> missing,
        List<string> unsupported,
        List<string> skipped)
    {
        var lines = new List<string>
        {
            "# Mutagen Plugin Text Tool Report",
            "",
            $"- Input plugin: {Relative(projectRoot, inputPlugin)}",
            $"- Translation JSONL: {Relative(projectRoot, translationJsonl)}",
            $"- Output plugin: {Relative(projectRoot, outputPlugin)}",
            $"- Dry run: {dryRun}",
            $"- Candidate rows: {candidateCount}",
            $"- Applied rows: {applied.Count}",
            $"- Missing rows: {missing.Count}",
            $"- Unsupported rows: {unsupported.Count}",
            "",
            "## Applied",
            "",
        };
        lines.AddRange(applied.Count == 0 ? ["No applied rows."] : applied.Select(item => $"- {item}"));
        lines.Add("");
        lines.Add("## Missing");
        lines.Add("");
        lines.AddRange(missing.Count == 0 ? ["No missing rows."] : missing.Select(item => $"- {item}"));
        lines.Add("");
        lines.Add("## Unsupported");
        lines.Add("");
        lines.AddRange(unsupported.Count == 0 ? ["No unsupported rows."] : unsupported.Select(item => $"- {item}"));
        lines.Add("");
        lines.Add("## Notes");
        lines.Add("");
        lines.AddRange(skipped.Count == 0 ? ["No notes."] : skipped.Select(item => $"- {item}"));
        lines.Add("");
        lines.Add("## Safety");
        lines.Add("");
        lines.Add("- All paths were checked to be inside the project root.");
        lines.Add("- This tool does not read real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games paths.");
        lines.Add("- This tool writes only to the requested project-local output plugin path, and only when not in dry-run mode.");
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static bool SameEditorId(string? left, string? right)
    {
        return string.Equals(left ?? string.Empty, right ?? string.Empty, StringComparison.OrdinalIgnoreCase);
    }

    private static bool SameFormId(string? left, string? right)
    {
        return string.Equals(NormalizeFormId(left), NormalizeFormId(right), StringComparison.OrdinalIgnoreCase);
    }

    private static string NormalizeFormId(string? value)
    {
        var trimmed = (value ?? string.Empty).Trim();
        if (trimmed.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
        {
            trimmed = trimmed[2..];
        }
        trimmed = trimmed.ToUpperInvariant();
        if (trimmed.Length > 6)
        {
            trimmed = trimmed[^6..];
        }
        return trimmed.PadLeft(6, '0');
    }

    private static string Describe(TranslationRow row, string action)
    {
        return $"{row.RecordType} {row.FormId} {row.SubrecordType} {row.EditorId}: {action}";
    }

    private static string Require(string? value, string name)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException($"Missing required argument: {name}");
        }
        return value;
    }

    private static string FullPath(string path)
    {
        return Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    private static void EnsureInside(string child, string parent, string label)
    {
        var childFull = FullPath(child);
        var parentFull = FullPath(parent);
        if (!string.Equals(childFull, parentFull, StringComparison.OrdinalIgnoreCase)
            && !childFull.StartsWith(parentFull + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} is outside project root: {childFull}");
        }
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

    private static string Relative(string root, string path)
    {
        return Path.GetRelativePath(root, path).Replace('\\', '/');
    }

    private sealed class TranslationRow
    {
        [JsonPropertyName("record_type")]
        public string RecordType { get; set; } = "";

        [JsonPropertyName("form_id")]
        public string FormId { get; set; } = "";

        [JsonPropertyName("editor_id")]
        public string EditorId { get; set; } = "";

        [JsonPropertyName("subrecord_type")]
        public string SubrecordType { get; set; } = "";

        [JsonPropertyName("subrecord_index")]
        public int SubrecordIndex { get; set; }

        [JsonPropertyName("risk")]
        public string Risk { get; set; } = "";

        [JsonPropertyName("target")]
        public string Target { get; set; } = "";
    }

    private sealed class Options
    {
        public string Command { get; private set; } = "";
        public string? ProjectRoot { get; private set; }
        public string? InputPlugin { get; private set; }
        public string? TranslationJsonl { get; private set; }
        public string? OutputPlugin { get; private set; }
        public string? Report { get; private set; }
        public bool DryRun { get; private set; }

        public static Options Parse(string[] args)
        {
            var options = new Options();
            if (args.Length > 0)
            {
                options.Command = args[0];
            }
            for (var index = 1; index < args.Length; index++)
            {
                var arg = args[index];
                switch (arg)
                {
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
