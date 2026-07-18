using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

public static class Program
{
    private static readonly JsonSerializerOptions CompactJson = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static int Main(string[] args)
    {
        try
        {
            var options = Options.Parse(args);
            return options.Command switch
            {
                "inventory" => Inventory(options),
                "export" => Export(options),
                "apply" => Apply(options),
                "verify" => Verify(options),
                _ => Usage(),
            };
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }

    private static int Inventory(Options options)
    {
        RequireInventoryCapability(options.CapabilityLevel);
        var context = ResolveCommon(options);
        var document = ReadDocument(context, options);
        var metadata = Metadata(context, document, options.SourceEncoding);
        if (!string.IsNullOrWhiteSpace(options.OutputJson))
        {
            var outputJson = OutputPath(context.Root, options.OutputJson, "--output-json", ".json");
            RequireDistinct(context.Input, outputJson, "Inventory JSON cannot overwrite the input table.");
            WriteTextAtomic(
                outputJson,
                writer => writer.Write(JsonSerializer.Serialize(metadata, new JsonSerializerOptions { WriteIndented = true })));
        }
        WriteReport(context, document, options, "inventory", replacementCount: null);
        return 0;
    }

    private static int Export(Options options)
    {
        RequireReadCapability(options.CapabilityLevel);
        var context = ResolveCommon(options);
        RequireSourceLanguage(context.Identity, options.SourceLanguage);
        var document = ReadDocument(context, options);
        var output = OutputPath(context.Root, options.OutputJsonl, "--output-jsonl", ".jsonl");
        RequireDistinct(context.Input, output, "Export JSONL cannot overwrite the input table.");
        var sourceHash = Sha256(context.Input);
        var sourcePath = RelativePath(context.Root, context.Input);
        WriteTextAtomic(
            output,
            writer =>
            {
                foreach (var entry in document.Entries)
                {
                    var row = new Dictionary<string, object?>
                    {
                        ["schema_version"] = StringTableTranslations.SchemaVersion,
                        ["game_id"] = options.Game,
                        ["plugin_basename"] = context.Identity.PluginBasename,
                        ["table_type"] = StringTableCodec.TypeName(document.Type),
                        ["source_language"] = options.SourceLanguage,
                        ["string_id"] = entry.Id,
                        ["Type"] = StringTableCodec.TypeName(document.Type).ToUpperInvariant(),
                        ["Source"] = entry.Value,
                        ["Result"] = "",
                        ["risk"] = "candidate",
                        ["source_table_path"] = sourcePath,
                        ["source_table_sha256"] = sourceHash,
                    };
                    writer.WriteLine(JsonSerializer.Serialize(row, CompactJson));
                }
            });
        WriteReport(context, document, options, "export", replacementCount: null);
        return 0;
    }

    private static int Apply(Options options)
    {
        RequireWriteCapability(options.CapabilityLevel);
        var context = ResolveCommon(options);
        RequireSourceLanguage(context.Identity, options.SourceLanguage);
        var output = OutputPath(context.Root, options.OutputTable, "--output-table", context.Input.Extension);
        ValidateTargetFilename(context.Identity, output, options.TargetLanguage);
        var translation = InputPath(context.Root, options.TranslationJsonl, "--translation-jsonl");
        RequireDistinct(context.Input, output, "Output table cannot overwrite the input table.");
        RequireDistinct(translation, output, "Output table cannot overwrite the translation JSONL.");

        var targetEncoding = StringTableCodec.StrictEncoding(options.TargetEncoding);
        var source = StringTableCodec.Read(
            context.Input.FullName,
            options.SourceEncoding,
            options.MaxEntries,
            options.MaxFileBytes);
        var sourceValues = source.Entries.ToDictionary(entry => entry.Id, entry => entry.Value);
        var translations = StringTableTranslations.LoadAndValidate(
            translation.FullName,
            options.Game,
            context.Identity,
            options.SourceLanguage,
            RelativePath(context.Root, context.Input),
            Sha256(context.Input),
            sourceValues,
            targetEncoding);
        var outputEntries = source.Entries
            .Select(entry => (
                entry.Id,
                translations.Replacements.TryGetValue(entry.Id, out var target)
                    ? target
                    : entry.Value))
            .ToArray();
        WriteBytesAtomic(output, StringTableCodec.Write(source.Type, outputEntries, targetEncoding));
        WriteReport(context, source, options, "apply", translations.Replacements.Count, output);
        return 0;
    }

    private static int Verify(Options options)
    {
        RequireReadCapability(options.CapabilityLevel);
        var context = ResolveCommon(options);
        RequireSourceLanguage(context.Identity, options.SourceLanguage);
        var output = InputPath(context.Root, options.OutputTable, "--output-table");
        if (!string.Equals(output.Extension, context.Input.Extension, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Input and output string-table types do not match.");
        }
        ValidateTargetFilename(context.Identity, output, options.TargetLanguage);
        var translation = InputPath(context.Root, options.TranslationJsonl, "--translation-jsonl");
        RequireDistinct(context.Input, output, "Verify requires separate input and output tables.");

        var targetEncoding = StringTableCodec.StrictEncoding(options.TargetEncoding);
        var source = StringTableCodec.Read(
            context.Input.FullName,
            options.SourceEncoding,
            options.MaxEntries,
            options.MaxFileBytes);
        var result = StringTableCodec.Read(
            output.FullName,
            options.TargetEncoding,
            options.MaxEntries,
            options.MaxFileBytes);
        var sourceValues = source.Entries.ToDictionary(entry => entry.Id, entry => entry.Value);
        var translations = StringTableTranslations.LoadAndValidate(
            translation.FullName,
            options.Game,
            context.Identity,
            options.SourceLanguage,
            RelativePath(context.Root, context.Input),
            Sha256(context.Input),
            sourceValues,
            targetEncoding);
        VerifyLogicalOutput(source, result, translations.Replacements);
        WriteReport(context, result, options, "verify", translations.Replacements.Count, output);
        return 0;
    }

    private static void VerifyLogicalOutput(
        StringTableDocument source,
        StringTableDocument output,
        IReadOnlyDictionary<uint, string> replacements)
    {
        if (source.Type != output.Type || source.Entries.Count != output.Entries.Count)
        {
            throw new InvalidDataException("String-table type or entry count changed.");
        }
        var outputValues = output.Entries.ToDictionary(entry => entry.Id, entry => entry.Value);
        if (!source.Entries.Select(entry => entry.Id).ToHashSet().SetEquals(outputValues.Keys))
        {
            throw new InvalidDataException("String-table ID set changed.");
        }
        foreach (var entry in source.Entries)
        {
            var expected = replacements.TryGetValue(entry.Id, out var target) ? target : entry.Value;
            if (!string.Equals(outputValues[entry.Id], expected, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    replacements.ContainsKey(entry.Id)
                        ? $"Authorized target mismatch for string ID {entry.Id}."
                        : $"Unauthorized logical value changed for string ID {entry.Id}.");
            }
        }
    }

    private static CommonContext ResolveCommon(Options options)
    {
        var root = RequireRoot(options.ProjectRoot);
        var input = InputPath(root, options.InputTable, "--input-table");
        var report = OutputPath(root, options.Report, "--report", ".md");
        RequireDistinct(input, report, "Report cannot overwrite the input table.");
        var sourceLanguage = Require(options.SourceLanguage, "--source-language");
        var identity = StringTableFileIdentity.Parse(input.FullName, sourceLanguage);
        Require(options.Game, "--game");
        Require(options.SourceEncoding, "--source-encoding");
        if (options.Command is "apply" or "verify")
        {
            Require(options.TargetEncoding, "--target-encoding");
        }
        return new CommonContext(root, input, report, identity);
    }

    private static StringTableDocument ReadDocument(CommonContext context, Options options) =>
        StringTableCodec.Read(
            context.Input.FullName,
            options.SourceEncoding,
            options.MaxEntries,
            options.MaxFileBytes);

    private static Dictionary<string, object?> Metadata(
        CommonContext context,
        StringTableDocument document,
        string encoding) =>
        new()
        {
            ["schema_version"] = 1,
            ["plugin_basename"] = context.Identity.PluginBasename,
            ["language"] = context.Identity.Language,
            ["table_type"] = StringTableCodec.TypeName(document.Type),
            ["entry_count"] = document.Entries.Count,
            ["file_size"] = document.FileSize,
            ["data_size"] = document.DataSize,
            ["sha256"] = Sha256(context.Input),
            ["encoding"] = encoding,
        };

    private static void WriteReport(
        CommonContext context,
        StringTableDocument document,
        Options options,
        string operation,
        int? replacementCount,
        FileInfo? output = null)
    {
        var lines = new List<string>
        {
            "# Bethesda String Table Adapter",
            "",
            $"- Operation: {operation}",
            $"- game_id: {options.Game}",
            $"- capability_level: {options.CapabilityLevel}",
            $"- Input table: {RelativePath(context.Root, context.Input)}",
            $"- Plugin basename: {context.Identity.PluginBasename}",
            $"- Language: {context.Identity.Language}",
            $"- Table type: {StringTableCodec.TypeName(document.Type)}",
            $"- Entry count: {document.Entries.Count}",
            $"- File size: {document.FileSize}",
            $"- Data size: {document.DataSize}",
            $"- Input SHA256: {Sha256(context.Input)}",
            $"- Source encoding: {options.SourceEncoding}",
        };
        if (!string.IsNullOrWhiteSpace(options.TargetEncoding))
        {
            lines.Add($"- Target encoding: {options.TargetEncoding}");
        }
        if (!string.IsNullOrWhiteSpace(options.TargetLanguage))
        {
            lines.Add($"- Target language: {options.TargetLanguage}");
        }
        if (replacementCount is not null)
        {
            lines.Add($"- Authorized replacements: {replacementCount.Value}");
        }
        if (output is not null)
        {
            lines.Add($"- Output table: {RelativePath(context.Root, output)}");
            lines.Add($"- Output SHA256: {Sha256(output)}");
        }
        lines.Add("");
        WriteTextAtomic(context.Report, writer => writer.Write(string.Join("\n", lines)));
    }

    private static void RequireSourceLanguage(StringTableFileIdentity identity, string language)
    {
        var expected = Require(language, "--source-language");
        if (!string.Equals(identity.Language, expected, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"Source filename language '{identity.Language}' does not match '{expected}'.");
        }
    }

    private static void ValidateTargetFilename(
        StringTableFileIdentity identity,
        FileInfo output,
        string language)
    {
        var expected = identity.FilenameForLanguage(Require(language, "--target-language"));
        if (!string.Equals(output.Name, expected, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"Output filename must use the target-language mapping: {expected}");
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine("Usage:");
        Console.Error.WriteLine("  BethesdaStringTableTool inventory --game <id> --capability-level <level> --project-root <path> --input-table <path> --source-encoding <name> --source-language <token> --report <path> [--output-json <path>]");
        Console.Error.WriteLine("  BethesdaStringTableTool export --game <id> --capability-level <level> --project-root <path> --input-table <path> --source-encoding <name> --source-language <token> --output-jsonl <path> --report <path>");
        Console.Error.WriteLine("  BethesdaStringTableTool apply --game <id> --capability-level <level> --project-root <path> --input-table <path> --source-encoding <name> --target-encoding <name> --source-language <token> --target-language <token> --translation-jsonl <path> --output-table <path> --report <path>");
        Console.Error.WriteLine("  BethesdaStringTableTool verify --game <id> --capability-level <level> --project-root <path> --input-table <path> --source-encoding <name> --target-encoding <name> --source-language <token> --target-language <token> --translation-jsonl <path> --output-table <path> --report <path>");
        return 2;
    }

    private static void RequireInventoryCapability(string value)
    {
        if (value is not ("inventory_only" or "read_only" or "experimental_write" or "stable"))
        {
            throw new InvalidDataException(
                $"String-table capability level '{value}' does not permit inventory.");
        }
    }

    private static void RequireReadCapability(string value)
    {
        if (value is not ("read_only" or "experimental_write" or "stable"))
        {
            throw new InvalidDataException(
                $"String-table capability level '{value}' does not permit export.");
        }
    }

    private static void RequireWriteCapability(string value)
    {
        if (value is not ("experimental_write" or "stable"))
        {
            throw new InvalidDataException(
                $"String-table capability level '{value}' does not permit writeback.");
        }
    }

    private static DirectoryInfo RequireRoot(string value)
    {
        var root = new DirectoryInfo(Path.GetFullPath(Require(value, "--project-root")));
        if (!root.Exists)
        {
            throw new DirectoryNotFoundException($"Project root does not exist: {root.FullName}");
        }
        return root;
    }

    private static FileInfo InputPath(DirectoryInfo root, string value, string label)
    {
        var path = PathUnderRoot(root, value, label);
        if (!path.Exists)
        {
            throw new FileNotFoundException($"{label} does not exist: {path.FullName}");
        }
        return path;
    }

    private static FileInfo OutputPath(
        DirectoryInfo root,
        string value,
        string label,
        string extension)
    {
        var path = PathUnderRoot(root, value, label);
        if (!string.Equals(path.Extension, extension, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException($"{label} must use the {extension} extension.");
        }
        return path;
    }

    private static FileInfo PathUnderRoot(DirectoryInfo root, string value, string label)
    {
        var path = new FileInfo(Path.GetFullPath(Require(value, label)));
        var relative = Path.GetRelativePath(root.FullName, path.FullName);
        if (Path.IsPathRooted(relative)
            || relative.Equals("..", StringComparison.Ordinal)
            || relative.StartsWith($"..{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
        {
            throw new InvalidDataException($"{label} must stay under the project root.");
        }
        return path;
    }

    private static string RelativePath(DirectoryInfo root, FileInfo path) =>
        Path.GetRelativePath(root.FullName, path.FullName).Replace('\\', '/');

    private static void RequireDistinct(FileInfo left, FileInfo right, string message)
    {
        if (string.Equals(left.FullName, right.FullName, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(message);
        }
    }

    private static string Sha256(FileInfo path)
    {
        using var stream = path.OpenRead();
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    private static void WriteBytesAtomic(FileInfo output, byte[] bytes)
    {
        output.Directory?.Create();
        var temporary = Path.Combine(
            output.DirectoryName!,
            $".{output.Name}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(
                temporary,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                64 * 1024,
                FileOptions.WriteThrough))
            {
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, output.FullName, overwrite: true);
        }
        finally
        {
            File.Delete(temporary);
        }
    }

    private static void WriteTextAtomic(FileInfo output, Action<StreamWriter> write)
    {
        output.Directory?.Create();
        var temporary = Path.Combine(
            output.DirectoryName!,
            $".{output.Name}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(
                temporary,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                64 * 1024,
                FileOptions.WriteThrough))
            using (var writer = new StreamWriter(stream, new UTF8Encoding(false)))
            {
                write(writer);
                writer.Flush();
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, output.FullName, overwrite: true);
        }
        finally
        {
            File.Delete(temporary);
        }
    }

    private static string Require(string value, string label)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException($"{label} is required.");
        }
        return value.Trim();
    }

    private sealed record CommonContext(
        DirectoryInfo Root,
        FileInfo Input,
        FileInfo Report,
        StringTableFileIdentity Identity);

    private sealed class Options
    {
        public string Command { get; private set; } = "";
        public string Game { get; private set; } = "";
        public string CapabilityLevel { get; private set; } = "";
        public string ProjectRoot { get; private set; } = "";
        public string InputTable { get; private set; } = "";
        public string SourceEncoding { get; private set; } = "";
        public string TargetEncoding { get; private set; } = "";
        public string SourceLanguage { get; private set; } = "";
        public string TargetLanguage { get; private set; } = "";
        public string OutputJson { get; private set; } = "";
        public string OutputJsonl { get; private set; } = "";
        public string TranslationJsonl { get; private set; } = "";
        public string OutputTable { get; private set; } = "";
        public string Report { get; private set; } = "";
        public int MaxEntries { get; private set; } = StringTableCodec.DefaultMaxEntries;
        public long MaxFileBytes { get; private set; } = StringTableCodec.DefaultMaxFileBytes;

        public static Options Parse(string[] args)
        {
            if (args.Length == 0)
            {
                return new Options();
            }
            var options = new Options { Command = args[0].ToLowerInvariant() };
            for (var index = 1; index < args.Length; index += 2)
            {
                if (index + 1 >= args.Length)
                {
                    throw new ArgumentException($"Missing value for {args[index]}.");
                }
                var key = args[index];
                var value = args[index + 1];
                switch (key)
                {
                    case "--game": options.Game = value; break;
                    case "--capability-level": options.CapabilityLevel = value; break;
                    case "--project-root": options.ProjectRoot = value; break;
                    case "--input-table": options.InputTable = value; break;
                    case "--source-encoding": options.SourceEncoding = value; break;
                    case "--target-encoding": options.TargetEncoding = value; break;
                    case "--source-language": options.SourceLanguage = value; break;
                    case "--target-language": options.TargetLanguage = value; break;
                    case "--output-json": options.OutputJson = value; break;
                    case "--output-jsonl": options.OutputJsonl = value; break;
                    case "--translation-jsonl": options.TranslationJsonl = value; break;
                    case "--output-table": options.OutputTable = value; break;
                    case "--report": options.Report = value; break;
                    case "--max-entries": options.MaxEntries = int.Parse(value); break;
                    case "--max-file-bytes": options.MaxFileBytes = long.Parse(value); break;
                    default: throw new ArgumentException($"Unknown argument: {key}");
                }
            }
            return options;
        }
    }
}
