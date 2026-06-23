using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Pex;

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
            Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
            var options = Options.Parse(args);
            return options.Command switch
            {
                "export" => Export(options),
                "apply" => Apply(options),
                _ => Usage(),
            };
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine("Usage:");
        Console.Error.WriteLine("  SkyrimPexStringTool export --project-root <path> --input-pex <path> --output-jsonl <path> --report <path>");
        Console.Error.WriteLine("  SkyrimPexStringTool apply --project-root <path> --input-pex <path> --translation-jsonl <path> --output-pex <path> --report <path> [--dry-run]");
        return 2;
    }

    private static int Export(Options options)
    {
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPex = FullPath(Require(options.InputPex, "--input-pex"));
        var outputJsonl = FullPath(Require(options.OutputJsonl, "--output-jsonl"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureProjectPath(inputPex, projectRoot, "input PEX");
        EnsureProjectPath(outputJsonl, projectRoot, "output JSONL");
        EnsureProjectPath(reportPath, projectRoot, "report");
        EnsurePexExtension(inputPex, "input PEX");
        EnsureNoRiskyMarker(inputPex);
        EnsureNoRiskyMarker(outputJsonl);
        EnsureNoRiskyMarker(reportPath);

        Directory.CreateDirectory(Path.GetDirectoryName(outputJsonl)!);
        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

        var pex = PexFile.CreateFromFile(inputPex, GameCategory.Skyrim);
        var occurrences = EnumerateInstructionStrings(pex, Path.GetFileName(inputPex)).ToList();
        WriteJsonl(outputJsonl, occurrences.Select(ExportRow.FromOccurrence));
        WriteExportReport(reportPath, projectRoot, inputPex, outputJsonl, occurrences);

        Console.WriteLine($"PEX export JSONL: {outputJsonl}");
        Console.WriteLine($"PEX export report: {reportPath}");
        Console.WriteLine($"Instruction string occurrences: {occurrences.Count}");
        return 0;
    }

    private static int Apply(Options options)
    {
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPex = FullPath(Require(options.InputPex, "--input-pex"));
        var translationJsonl = FullPath(Require(options.TranslationJsonl, "--translation-jsonl"));
        var outputPex = FullPath(Require(options.OutputPex, "--output-pex"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureProjectPath(inputPex, projectRoot, "input PEX");
        EnsureProjectPath(translationJsonl, projectRoot, "translation JSONL");
        EnsureProjectPath(outputPex, projectRoot, "output PEX");
        EnsureProjectPath(reportPath, projectRoot, "report");
        EnsurePexExtension(inputPex, "input PEX");
        EnsurePexExtension(outputPex, "output PEX");
        EnsureNoRiskyMarker(inputPex);
        EnsureNoRiskyMarker(translationJsonl);
        EnsureNoRiskyMarker(outputPex);
        EnsureNoRiskyMarker(reportPath);

        Directory.CreateDirectory(Path.GetDirectoryName(outputPex)!);
        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

        var fileName = Path.GetFileName(inputPex);
        var rows = ReadTranslationRows(translationJsonl, fileName);
        var conflicts = FindConflicts(rows);
        var usableRows = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Where(row => !string.IsNullOrWhiteSpace(row.Target))
            .Where(row => !string.Equals(row.Source, row.Target, StringComparison.Ordinal))
            .Where(row => !TranslationRowProtectsSource(row))
            .Where(row => !conflicts.Contains(row.Source))
            .ToList();

        var pex = PexFile.CreateFromFile(inputPex, GameCategory.Skyrim);
        conflicts.UnionWith(FindNonInstructionSourceConflicts(pex, usableRows));
        conflicts.UnionWith(FindProtectedInstructionSourceConflicts(pex, usableRows));
        usableRows = usableRows
            .Where(row => !conflicts.Contains(row.Source))
            .ToList();
        var applyResult = ApplyRows(pex, fileName, usableRows, options.DryRun);

        if (!options.DryRun)
        {
            PatchPexStringTable(inputPex, outputPex, usableRows);
            PexFile.CreateFromFile(outputPex, GameCategory.Skyrim);
        }

        WriteApplyReport(reportPath, projectRoot, inputPex, translationJsonl, outputPex, options.DryRun, rows, usableRows, conflicts, applyResult);

        Console.WriteLine($"PEX apply report: {reportPath}");
        Console.WriteLine($"Rows parsed: {rows.Count}");
        Console.WriteLine($"Usable rows: {usableRows.Count}");
        Console.WriteLine($"Instruction string replacements: {applyResult.Replacements.Count}");

        if (conflicts.Count > 0 || applyResult.MissingRows.Count > 0)
        {
            return 2;
        }
        return 0;
    }

    private static void PatchPexStringTable(string inputPex, string outputPex, IReadOnlyCollection<TranslationRow> usableRows)
    {
        var replacements = usableRows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Where(row => !string.IsNullOrWhiteSpace(row.Target))
            .GroupBy(row => row.Source!, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.First().Target!, StringComparer.Ordinal);

        var inputBytes = File.ReadAllBytes(inputPex);
        using var input = new MemoryStream(inputBytes, writable: false);
        using var reader = new BinaryReader(input, Encoding.UTF8, leaveOpen: true);
        using var output = new MemoryStream(inputBytes.Length);
        using var writer = new BinaryWriter(output, Encoding.UTF8, leaveOpen: true);

        SkipPexHeader(reader);
        var stringCountPosition = checked((int)input.Position);
        var stringCount = ReadUInt16BigEndian(reader);
        output.Write(inputBytes, 0, stringCountPosition);
        WriteUInt16BigEndian(writer, stringCount);

        var cp1252 = Encoding.GetEncoding(1252);
        for (var i = 0; i < stringCount; i++)
        {
            var length = ReadUInt16BigEndian(reader);
            var bytes = reader.ReadBytes(length);
            if (bytes.Length != length)
            {
                throw new InvalidDataException($"PEX string table ended unexpectedly at index {i}.");
            }

            var utf8Text = Encoding.UTF8.GetString(bytes);
            var cp1252Text = cp1252.GetString(bytes);
            if (replacements.TryGetValue(utf8Text, out var target) ||
                replacements.TryGetValue(cp1252Text, out target))
            {
                var targetBytes = Encoding.UTF8.GetBytes(target);
                if (targetBytes.Length > ushort.MaxValue)
                {
                    throw new InvalidDataException($"Replacement string is too long for PEX string table: {utf8Text}");
                }
                WriteUInt16BigEndian(writer, (ushort)targetBytes.Length);
                writer.Write(targetBytes);
                continue;
            }

            WriteUInt16BigEndian(writer, length);
            writer.Write(bytes);
        }

        var restOffset = checked((int)input.Position);
        output.Write(inputBytes, restOffset, inputBytes.Length - restOffset);

        Directory.CreateDirectory(Path.GetDirectoryName(outputPex)!);
        File.WriteAllBytes(outputPex, output.ToArray());
    }

    private static void SkipPexHeader(BinaryReader reader)
    {
        const uint pexMagic = 0xFA57C0DE;
        var magic = ReadUInt32BigEndian(reader);
        if (magic != pexMagic)
        {
            throw new InvalidDataException($"File does not have fast code! Magic does not match {pexMagic:x8} is {magic:x8}");
        }

        reader.ReadByte();
        reader.ReadByte();
        ReadUInt16BigEndian(reader);
        ReadUInt64BigEndian(reader);
        SkipPrependedString(reader);
        SkipPrependedString(reader);
        SkipPrependedString(reader);
    }

    private static void SkipPrependedString(BinaryReader reader)
    {
        var length = ReadUInt16BigEndian(reader);
        var bytes = reader.ReadBytes(length);
        if (bytes.Length != length)
        {
            throw new InvalidDataException("PEX header string ended unexpectedly.");
        }
    }

    private static ushort ReadUInt16BigEndian(BinaryReader reader)
    {
        var bytes = reader.ReadBytes(sizeof(ushort));
        if (bytes.Length != sizeof(ushort))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt16.");
        }
        return BinaryPrimitives.ReadUInt16BigEndian(bytes);
    }

    private static uint ReadUInt32BigEndian(BinaryReader reader)
    {
        var bytes = reader.ReadBytes(sizeof(uint));
        if (bytes.Length != sizeof(uint))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt32.");
        }
        return BinaryPrimitives.ReadUInt32BigEndian(bytes);
    }

    private static ulong ReadUInt64BigEndian(BinaryReader reader)
    {
        var bytes = reader.ReadBytes(sizeof(ulong));
        if (bytes.Length != sizeof(ulong))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt64.");
        }
        return BinaryPrimitives.ReadUInt64BigEndian(bytes);
    }

    private static void WriteUInt16BigEndian(BinaryWriter writer, ushort value)
    {
        Span<byte> bytes = stackalloc byte[sizeof(ushort)];
        BinaryPrimitives.WriteUInt16BigEndian(bytes, value);
        writer.Write(bytes);
    }

    private static ApplyResult ApplyRows(PexFile pex, string fileName, List<TranslationRow> rows, bool dryRun)
    {
        var result = new ApplyResult();
        var exactRows = rows
            .GroupBy(row => row.Source, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.Ordinal);
        var ignoreCaseRows = rows
            .Where(row => row.IgnoreCase)
            .GroupBy(row => row.Source, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.OrdinalIgnoreCase);

        foreach (var occurrence in EnumerateInstructionStrings(pex, fileName))
        {
            if (string.IsNullOrEmpty(occurrence.Argument.StringValue))
            {
                continue;
            }

            var source = occurrence.Argument.StringValue;
            TranslationRow? row = null;
            if (!exactRows.TryGetValue(source, out row))
            {
                ignoreCaseRows.TryGetValue(source, out row);
            }
            if (row is null)
            {
                continue;
            }

            result.FoundSources.Add(row.Source);
            result.Replacements.Add(new Replacement(
                occurrence.ObjectName,
                occurrence.StateName,
                occurrence.FunctionName,
                occurrence.OpCode,
                occurrence.InstructionIndex,
                occurrence.ArgumentIndex,
                source,
                row.Target));

            if (!dryRun)
            {
                occurrence.Argument.StringValue = row.Target;
            }
        }

        foreach (var row in rows)
        {
            if (!result.FoundSources.Contains(row.Source))
            {
                result.MissingRows.Add(row);
            }
        }

        return result;
    }

    private static IEnumerable<PexStringOccurrence> EnumerateInstructionStrings(PexFile pex, string fileName)
    {
        foreach (var pexObject in pex.Objects)
        {
            foreach (var property in pexObject.Properties)
            {
                if (property.ReadHandler is not null)
                {
                    foreach (var occurrence in EnumerateFunctionStrings(fileName, pexObject.Name ?? "", "", $"{property.Name}.get", property.ReadHandler))
                    {
                        yield return occurrence;
                    }
                }
                if (property.WriteHandler is not null)
                {
                    foreach (var occurrence in EnumerateFunctionStrings(fileName, pexObject.Name ?? "", "", $"{property.Name}.set", property.WriteHandler))
                    {
                        yield return occurrence;
                    }
                }
            }

            foreach (var state in pexObject.States)
            {
                foreach (var namedFunction in state.Functions)
                {
                    if (namedFunction.Function is null)
                    {
                        continue;
                    }
                    foreach (var occurrence in EnumerateFunctionStrings(
                                 fileName,
                                 pexObject.Name ?? "",
                                 state.Name ?? "",
                                 namedFunction.FunctionName ?? "",
                                 namedFunction.Function))
                    {
                        yield return occurrence;
                    }
                }
            }
        }
    }

    private static IEnumerable<PexStringOccurrence> EnumerateFunctionStrings(
        string fileName,
        string objectName,
        string stateName,
        string functionName,
        PexObjectFunction function)
    {
        for (var instructionIndex = 0; instructionIndex < function.Instructions.Count; instructionIndex++)
        {
            var instruction = function.Instructions[instructionIndex];
            for (var argumentIndex = 0; argumentIndex < instruction.Arguments.Count; argumentIndex++)
            {
                var argument = instruction.Arguments[argumentIndex];
                if (argument.VariableType != VariableType.String || string.IsNullOrEmpty(argument.StringValue))
                {
                    continue;
                }

                yield return new PexStringOccurrence(
                    fileName,
                    objectName,
                    stateName,
                    functionName,
                    instruction.OpCode.ToString(),
                    instructionIndex,
                    argumentIndex,
                    argument);
            }
        }
    }

    private static List<TranslationRow> ReadTranslationRows(string translationJsonl, string fileName)
    {
        var rows = new List<TranslationRow>();
        var lineNumber = 0;
        foreach (var line in File.ReadLines(translationJsonl, Encoding.UTF8))
        {
            lineNumber++;
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            var modName = GetString(root, "ModName", "mod_name", "file_name", "file");
            if (!string.IsNullOrWhiteSpace(modName)
                && !string.Equals(modName, fileName, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var source = DecodeControlEscapes(GetString(root, "Source", "source", "original", "text"));
            var target = DecodeControlEscapes(GetString(root, "Result", "result", "Target", "target", "translation"));
            var ignoreCase = GetBool(root, "IgnoreCase", "ignore_case");
            var risk = GetString(root, "risk", "Risk");
            var opcode = GetString(root, "opcode", "Opcode", "op", "Op");
            rows.Add(new TranslationRow(lineNumber, modName, source, target, ignoreCase, risk, opcode));
        }
        return rows;
    }

    private static bool TranslationRowProtectsSource(TranslationRow row)
    {
        var risk = row.Risk.Trim().ToLowerInvariant();
        var opcode = row.OpCode.Trim().ToUpperInvariant();
        return risk is "blocked" or "logic" or "manual" or "protected" or "protected-logic" or "review" or "unsafe"
            || opcode.StartsWith("CMP_", StringComparison.Ordinal);
    }

    private static HashSet<string> FindConflicts(IEnumerable<TranslationRow> rows)
    {
        var conflicts = new HashSet<string>(StringComparer.Ordinal);
        foreach (var group in rows
                     .Where(row => !string.IsNullOrWhiteSpace(row.Source))
                     .Where(row => !string.IsNullOrWhiteSpace(row.Target))
                     .GroupBy(row => row.Source, StringComparer.Ordinal))
        {
            if (group.Select(row => row.Target).Distinct(StringComparer.Ordinal).Skip(1).Any())
            {
                conflicts.Add(group.Key);
            }
        }
        return conflicts;
    }

    private static HashSet<string> FindNonInstructionSourceConflicts(PexFile pex, IEnumerable<TranslationRow> rows)
    {
        var sources = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Select(row => row.Source!)
            .ToHashSet(StringComparer.Ordinal);
        if (sources.Count == 0)
        {
            return [];
        }

        var metadataStrings = EnumerateNonInstructionStrings(pex).ToHashSet(StringComparer.Ordinal);
        sources.IntersectWith(metadataStrings);
        return sources;
    }

    private static HashSet<string> FindProtectedInstructionSourceConflicts(PexFile pex, IEnumerable<TranslationRow> rows)
    {
        var sources = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Select(row => row.Source!)
            .ToHashSet(StringComparer.Ordinal);
        if (sources.Count == 0)
        {
            return [];
        }

        var protectedSources = EnumerateInstructionStrings(pex, "")
            .Where(occurrence => occurrence.OpCode.StartsWith("CMP_", StringComparison.Ordinal))
            .Select(occurrence => occurrence.Text)
            .ToHashSet(StringComparer.Ordinal);
        sources.IntersectWith(protectedSources);
        return sources;
    }

    private static IEnumerable<string> EnumerateNonInstructionStrings(PexFile pex)
    {
        foreach (var userFlag in pex.UserFlags)
        {
            if (!string.IsNullOrEmpty(userFlag))
            {
                yield return userFlag;
            }
        }

        foreach (var pexObject in pex.Objects)
        {
            foreach (var value in Strings(pexObject.Name, pexObject.ParentClassName, pexObject.DocString, pexObject.AutoStateName))
            {
                yield return value;
            }
            foreach (var structInfo in pexObject.StructInfos)
            {
                foreach (var value in Strings(structInfo.Name))
                {
                    yield return value;
                }
                foreach (var member in structInfo.Members)
                {
                    foreach (var value in Strings(member.Name, member.TypeName, member.DocString, member.Value?.StringValue))
                    {
                        yield return value;
                    }
                }
            }
            foreach (var variable in pexObject.Variables)
            {
                foreach (var value in Strings(variable.Name, variable.TypeName, variable.VariableData?.StringValue))
                {
                    yield return value;
                }
            }
            foreach (var property in pexObject.Properties)
            {
                foreach (var value in Strings(property.Name, property.TypeName, property.DocString, property.AutoVarName))
                {
                    yield return value;
                }
            }
            foreach (var state in pexObject.States)
            {
                foreach (var value in Strings(state.Name))
                {
                    yield return value;
                }
                foreach (var namedFunction in state.Functions)
                {
                    foreach (var value in Strings(namedFunction.FunctionName))
                    {
                        yield return value;
                    }
                    var function = namedFunction.Function;
                    if (function is null)
                    {
                        continue;
                    }
                    foreach (var value in Strings(function.ReturnTypeName, function.DocString))
                    {
                        yield return value;
                    }
                    foreach (var parameter in function.Parameters)
                    {
                        foreach (var value in Strings(parameter.Name, parameter.TypeName))
                        {
                            yield return value;
                        }
                    }
                    foreach (var local in function.Locals)
                    {
                        foreach (var value in Strings(local.Name, local.TypeName))
                        {
                            yield return value;
                        }
                    }
                }
            }
        }
    }

    private static IEnumerable<string> Strings(params string?[] values)
    {
        foreach (var value in values)
        {
            if (!string.IsNullOrEmpty(value))
            {
                yield return value;
            }
        }
    }

    private static string GetString(JsonElement root, params string[] names)
    {
        foreach (var name in names)
        {
            if (root.TryGetProperty(name, out var value) && value.ValueKind != JsonValueKind.Null)
            {
                return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
            }
        }
        return "";
    }

    private static bool GetBool(JsonElement root, params string[] names)
    {
        foreach (var name in names)
        {
            if (!root.TryGetProperty(name, out var value))
            {
                continue;
            }
            if (value.ValueKind is JsonValueKind.True)
            {
                return true;
            }
            if (value.ValueKind is JsonValueKind.False)
            {
                return false;
            }
            if (value.ValueKind is JsonValueKind.Number && value.TryGetInt32(out var number))
            {
                return number != 0;
            }
            if (value.ValueKind is JsonValueKind.String && bool.TryParse(value.GetString(), out var parsed))
            {
                return parsed;
            }
        }
        return false;
    }

    private static string DecodeControlEscapes(string value)
    {
        return value
            .Replace("\\r\\n", "\r\n", StringComparison.Ordinal)
            .Replace("\\n", "\n", StringComparison.Ordinal)
            .Replace("\\r", "\r", StringComparison.Ordinal)
            .Replace("\\t", "\t", StringComparison.Ordinal);
    }

    private static string RepairUtf8Mojibake(string value)
    {
        if (string.IsNullOrEmpty(value) || ContainsCjk(value) || !HasUtf8MojibakeMarker(value))
        {
            return value;
        }

        try
        {
            var bytes = Encoding.GetEncoding(1252).GetBytes(value);
            var repaired = Encoding.UTF8.GetString(bytes);
            if (repaired.Contains('\uFFFD', StringComparison.Ordinal))
            {
                return value;
            }
            return ContainsCjk(repaired) ? repaired : value;
        }
        catch
        {
            return value;
        }
    }

    private static bool HasUtf8MojibakeMarker(string value)
    {
        foreach (var ch in value)
        {
            if (ch is 'Ã' or 'Â' or 'Ä' or 'Å' or 'Æ' or 'Ç' or 'È' or 'É'
                or 'ã' or 'ä' or 'å' or 'æ' or 'ç' or 'è' or 'é'
                || (ch >= '\u0080' && ch <= '\u009F'))
            {
                return true;
            }
        }
        return false;
    }

    private static bool ContainsCjk(string value)
    {
        foreach (var ch in value)
        {
            if (ch >= '\u3400' && ch <= '\u9FFF')
            {
                return true;
            }
        }
        return false;
    }

    private static void WriteJsonl<T>(string path, IEnumerable<T> rows)
    {
        var options = new JsonSerializerOptions
        {
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        };
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        foreach (var row in rows)
        {
            writer.WriteLine(JsonSerializer.Serialize(row, options));
        }
    }

    private static void WriteExportReport(string reportPath, string projectRoot, string inputPex, string outputJsonl, List<PexStringOccurrence> occurrences)
    {
        var unique = occurrences.Select(item => item.Text).Distinct(StringComparer.Ordinal).Count();
        var candidate = occurrences.Count(item => ClassifyRisk(item.Text) == "candidate");
        var manualReview = occurrences.Count(item => ClassifyRisk(item.Text) == "manual-review");
        var protectedLogic = occurrences.Count(item => ClassifyRisk(item.Text) == "protected-logic");

        var lines = new List<string>
        {
            "# Mutagen PEX String Export Report",
            "",
            $"- Input PEX: {Relative(projectRoot, inputPex)}",
            $"- Output JSONL: {Relative(projectRoot, outputJsonl)}",
            $"- Checked at: {DateTime.Now:yyyy-MM-dd HH:mm:ss}",
            $"- Instruction string occurrences: {occurrences.Count}",
            $"- Unique instruction strings: {unique}",
            $"- Candidate occurrences: {candidate}",
            $"- Manual review occurrences: {manualReview}",
            $"- Protected/logic occurrences: {protectedLogic}",
            "",
            "## Scope",
            "",
            "- Exported only `VariableType.String` arguments inside PEX function instructions.",
            "- Did not export function names, variable names, property names, state names, identifiers, user flags, or debug symbols.",
            "- This is a read-only export; no PEX was modified.",
            "",
            "## Safety",
            "",
            "- All paths were checked to be inside the project root.",
            "- No real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.",
        };
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static void WriteApplyReport(
        string reportPath,
        string projectRoot,
        string inputPex,
        string translationJsonl,
        string outputPex,
        bool dryRun,
        List<TranslationRow> rows,
        List<TranslationRow> usableRows,
        HashSet<string> conflicts,
        ApplyResult result)
    {
        var lines = new List<string>
        {
            "# Mutagen PEX String Tool Report",
            "",
            $"- Input PEX: {Relative(projectRoot, inputPex)}",
            $"- Translation JSONL: {Relative(projectRoot, translationJsonl)}",
            $"- Output PEX: {Relative(projectRoot, outputPex)}",
            $"- Checked at: {DateTime.Now:yyyy-MM-dd HH:mm:ss}",
            $"- Dry run: {dryRun}",
            $"- Rows parsed for this PEX: {rows.Count}",
            $"- Usable rows: {usableRows.Count}",
            $"- Conflicting source rows: {conflicts.Count}",
            $"- Instruction string replacements: {result.Replacements.Count}",
            $"- Missing usable rows: {result.MissingRows.Count}",
            "",
            "## Replacements",
            "",
        };
        lines.AddRange(result.Replacements.Count == 0
            ? ["No replacements."]
            : result.Replacements.Select(item => $"- {item.ObjectName}.{item.FunctionName} [{item.OpCode} #{item.InstructionIndex}:{item.ArgumentIndex}]: `{EscapeInline(item.Source)}` -> `{EscapeInline(item.Target)}`"));
        lines.Add("");
        lines.Add("## Missing Rows");
        lines.Add("");
        lines.AddRange(result.MissingRows.Count == 0
            ? ["No missing rows."]
            : result.MissingRows.Select(item => $"- line {item.LineNumber}: `{EscapeInline(item.Source)}` -> `{EscapeInline(item.Target)}`"));
        lines.Add("");
        lines.Add("## Conflicts And Unsafe Global Sources");
        lines.Add("");
        lines.AddRange(conflicts.Count == 0
            ? ["No conflicting or unsafe rows."]
            : conflicts.Select(item => $"- `{EscapeInline(item)}` was skipped because it has multiple target translations, is also referenced outside instruction string arguments, or appears in protected comparison instructions."));
        lines.Add("");
        lines.Add("## Scope");
        lines.Add("");
        lines.Add("- Applied only to source strings first found in `VariableType.String` arguments inside PEX function instructions.");
        lines.Add("- Skipped any source string also found in non-instruction metadata or protected comparison instructions, to avoid globally changing names, identifiers, user flags, source file names, debug symbols, or logic comparisons.");
        lines.Add("- Patched only the PEX global string table, then re-read the output PEX to confirm it remains parseable.");
        lines.Add("");
        lines.Add("## Safety");
        lines.Add("");
        lines.Add("- All paths were checked to be inside the project root.");
        lines.Add("- Output PEX is a project-local copy generated by the controlled Mutagen adapter.");
        lines.Add("- No real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.");
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static string ClassifyRisk(string text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return "protected-logic";
        }
        if (text.StartsWith("$", StringComparison.Ordinal) || text.Contains("::", StringComparison.Ordinal) || text.Contains("\\", StringComparison.Ordinal) || text.Contains("/", StringComparison.Ordinal))
        {
            return "protected-logic";
        }
        if (text.All(ch => char.IsLetterOrDigit(ch) || ch == '_' || ch == '-'))
        {
            return "manual-review";
        }
        if (text.Length <= 3)
        {
            return "manual-review";
        }
        return "candidate";
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

    private static void EnsureProjectPath(string child, string projectRoot, string label)
    {
        var childFull = FullPath(child);
        var projectFull = FullPath(projectRoot);
        if (!string.Equals(childFull, projectFull, StringComparison.OrdinalIgnoreCase)
            && !childFull.StartsWith(projectFull + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} is outside project root: {childFull}");
        }
    }

    private static void EnsurePexExtension(string path, string label)
    {
        if (!string.Equals(Path.GetExtension(path), ".pex", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} must be a .pex file: {path}");
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

    private static string EscapeInline(string text)
    {
        return text
            .Replace("`", "\\`", StringComparison.Ordinal)
            .Replace("|", "\\|", StringComparison.Ordinal)
            .Replace("\r\n", "\\r\\n", StringComparison.Ordinal)
            .Replace("\n", "\\n", StringComparison.Ordinal)
            .Replace("\r", "\\r", StringComparison.Ordinal)
            .Replace("\t", "\\t", StringComparison.Ordinal);
    }

    private sealed record TranslationRow(int LineNumber, string ModName, string Source, string Target, bool IgnoreCase, string Risk, string OpCode);

    private sealed record Replacement(
        string ObjectName,
        string StateName,
        string FunctionName,
        string OpCode,
        int InstructionIndex,
        int ArgumentIndex,
        string Source,
        string Target);

    private sealed class ApplyResult
    {
        public HashSet<string> FoundSources { get; } = new(StringComparer.Ordinal);
        public List<Replacement> Replacements { get; } = [];
        public List<TranslationRow> MissingRows { get; } = [];
    }

    private sealed record PexStringOccurrence(
        string FileName,
        string ObjectName,
        string StateName,
        string FunctionName,
        string OpCode,
        int InstructionIndex,
        int ArgumentIndex,
        PexObjectVariableData Argument)
    {
        public string Text => RepairUtf8Mojibake(Argument.StringValue ?? "");
    }

    private sealed class ExportRow
    {
        public string ModName { get; init; } = "";
        public string Type { get; init; } = "PEX";
        public string Source { get; init; } = "";
        public string Result { get; init; } = "";
        public string risk { get; init; } = "";
        public string object_name { get; init; } = "";
        public string state_name { get; init; } = "";
        public string function_name { get; init; } = "";
        public string opcode { get; init; } = "";
        public int instruction_index { get; init; }
        public int argument_index { get; init; }
        public string notes { get; init; } = "";

        public static ExportRow FromOccurrence(PexStringOccurrence occurrence)
        {
            var risk = ClassifyRisk(occurrence.Text);
            return new ExportRow
            {
                ModName = occurrence.FileName,
                Source = occurrence.Text,
                risk = risk,
                object_name = occurrence.ObjectName,
                state_name = occurrence.StateName,
                function_name = occurrence.FunctionName,
                opcode = occurrence.OpCode,
                instruction_index = occurrence.InstructionIndex,
                argument_index = occurrence.ArgumentIndex,
                notes = risk switch
                {
                    "candidate" => "Instruction string candidate; confirm player visibility before writeback.",
                    "manual-review" => "Short or identifier-like string; do not translate unless confirmed visible.",
                    _ => "Protected or logic-like string; keep untranslated unless manually proven visible.",
                },
            };
        }
    }

    private sealed class Options
    {
        public string Command { get; private set; } = "";
        public string? ProjectRoot { get; private set; }
        public string? InputPex { get; private set; }
        public string? TranslationJsonl { get; private set; }
        public string? OutputPex { get; private set; }
        public string? OutputJsonl { get; private set; }
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
                    case "--input-pex":
                        options.InputPex = Next(args, ref index, arg);
                        break;
                    case "--translation-jsonl":
                        options.TranslationJsonl = Next(args, ref index, arg);
                        break;
                    case "--output-pex":
                        options.OutputPex = Next(args, ref index, arg);
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
