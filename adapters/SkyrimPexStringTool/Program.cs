using System.Buffers.Binary;
using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Pex;

internal sealed class Program
{
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);
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
        try
        {
            Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
            var options = Options.Parse(args);
            var game = Require(options.Game, "--game");
            var pexCategory = Require(options.PexCategory, "--pex-category");
            var capabilityLevel = Require(options.CapabilityLevel, "--capability-level");
            var category = GameFormat.ResolveCategory(pexCategory);
            return options.Command switch
            {
                "export" => Export(options, game, category, capabilityLevel),
                "apply" => Apply(options, game, category, capabilityLevel),
                "verify" => Verify(options, game, category, capabilityLevel),
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
        Console.Error.WriteLine("  SkyrimPexStringTool export --game <id> --pex-category <Skyrim|Fallout4> --capability-level <level> --project-root <path> --input-pex <path> --output-jsonl <path> --report <path> [--visible-api-registry <path>]");
        Console.Error.WriteLine("  SkyrimPexStringTool apply --game <id> --pex-category <Skyrim|Fallout4> --capability-level <experimental_write|stable> --project-root <path> --input-pex <path> --translation-jsonl <path> --output-pex <path> --report <path> [--visible-api-registry <path>] [--allow-experimental-writeback] [--dry-run]");
        Console.Error.WriteLine("  SkyrimPexStringTool verify --game <id> --pex-category <Skyrim|Fallout4> --capability-level <experimental_write|stable> --project-root <path> --input-pex <path> --translation-jsonl <path> --output-pex <path> --report <path> [--visible-api-registry <path>]");
        return 2;
    }

    private static void RequireReadCapability(string capabilityLevel)
    {
        if (capabilityLevel is not ("read_only" or "experimental_write" or "stable"))
        {
            throw new InvalidOperationException(
                $"PEX capability level '{capabilityLevel}' does not permit reading.");
        }
    }

    private static void RequireWriteCapability(string capabilityLevel)
    {
        if (capabilityLevel is not ("experimental_write" or "stable"))
        {
            throw new InvalidOperationException(
                $"PEX capability level '{capabilityLevel}' does not permit writeback.");
        }
    }

    private static int Export(
        Options options,
        string game,
        GameCategory category,
        string capabilityLevel)
    {
        RequireReadCapability(capabilityLevel);
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPex = FullPath(Require(options.InputPex, "--input-pex"));
        var outputJsonl = FullPath(Require(options.OutputJsonl, "--output-jsonl"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureProjectPath(inputPex, projectRoot, "input PEX");
        EnsureProjectPath(outputJsonl, projectRoot, "output JSONL");
        EnsureProjectPath(reportPath, projectRoot, "report");
        EnsurePexExtension(inputPex, "input PEX");
        EnsureJsonlExtension(outputJsonl, "output JSONL");
        EnsureMarkdownExtension(reportPath, "report");
        EnsureNoRiskyMarker(inputPex);
        EnsureNoRiskyMarker(outputJsonl);
        EnsureNoRiskyMarker(reportPath);
        EnsureDistinctPaths(
            ("input PEX", inputPex),
            ("output JSONL", outputJsonl),
            ("report", reportPath));

        Directory.CreateDirectory(Path.GetDirectoryName(outputJsonl)!);
        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);

        var classifier = PexCallSiteClassifier.ForGame(game, options.VisibleApiRegistry);
        var inputRead = PexCompatibilityReader.ReadFromFile(inputPex, category);
        var pex = inputRead.File;
        var occurrences = EnumerateInstructionStrings(
            pex,
            Path.GetFileName(inputPex),
            classifier).ToList();
        WriteJsonl(outputJsonl, occurrences.Select(occurrence => ExportRow.FromOccurrence(occurrence, game)));
        WriteExportReport(
            reportPath,
            projectRoot,
            inputPex,
            outputJsonl,
            occurrences,
            game,
            category,
            capabilityLevel,
            classifier,
            inputRead.Compatibility);

        Console.WriteLine($"PEX export JSONL: {outputJsonl}");
        Console.WriteLine($"PEX export report: {reportPath}");
        Console.WriteLine($"Instruction string occurrences: {occurrences.Count}");
        return 0;
    }

    private static int Verify(
        Options options,
        string game,
        GameCategory category,
        string capabilityLevel)
    {
        RequireWriteCapability(capabilityLevel);
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPex = FullPath(Require(options.InputPex, "--input-pex"));
        var translationJsonl = FullPath(Require(options.TranslationJsonl, "--translation-jsonl"));
        var outputPex = FullPath(Require(options.OutputPex, "--output-pex"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureProjectPath(inputPex, projectRoot, "input PEX");
        EnsureProjectPath(translationJsonl, projectRoot, "translation JSONL");
        EnsureProjectPath(outputPex, projectRoot, "output PEX");
        EnsureProjectPath(reportPath, projectRoot, "report");
        EnsurePexOutputPath(outputPex, projectRoot);
        EnsurePexExtension(inputPex, "input PEX");
        EnsurePexExtension(outputPex, "output PEX");
        EnsureJsonlExtension(translationJsonl, "translation JSONL");
        EnsureMarkdownExtension(reportPath, "report");
        EnsureNoRiskyMarker(inputPex);
        EnsureNoRiskyMarker(translationJsonl);
        EnsureNoRiskyMarker(outputPex);
        EnsureNoRiskyMarker(reportPath);
        EnsureDistinctPaths(
            ("input PEX", inputPex),
            ("translation JSONL", translationJsonl),
            ("output PEX", outputPex),
            ("report", reportPath));

        var fileName = Path.GetFileName(inputPex);
        var experimental = capabilityLevel == "experimental_write";
        var classifier = PexCallSiteClassifier.ForGame(game, options.VisibleApiRegistry);
        var errors = new List<string>();
        PexStructure? inputStructure = null;
        PexStructure? outputStructure = null;
        PexCompatibilityMetadata? inputCompatibility = null;
        PexCompatibilityMetadata? outputCompatibility = null;
        var rowsParsed = 0;
        var usableRowsCount = 0;
        var replacementCount = 0;

        try
        {
            var rows = ReadTranslationRows(translationJsonl, fileName, experimental);
            rowsParsed = rows.Count;
            var candidateRows = rows
                .Where(row => !string.IsNullOrWhiteSpace(row.Source))
                .Where(row => !string.IsNullOrWhiteSpace(row.Target))
                .Where(row => !string.Equals(row.Source, row.Target, StringComparison.Ordinal))
                .ToList();
            var conflicts = FindConflicts(candidateRows);
            var usableRows = candidateRows
                .Where(row => !TranslationRowProtectsSource(row))
                .Where(row => !conflicts.Contains(row.Source))
                .ToList();

            var inputRead = PexCompatibilityReader.ReadFromFile(inputPex, category);
            var outputRead = PexCompatibilityReader.ReadFromFile(outputPex, category);
            inputCompatibility = inputRead.Compatibility;
            outputCompatibility = outputRead.Compatibility;
            var input = inputRead.File;
            var output = outputRead.File;
            inputStructure = CountStructure(input);
            outputStructure = CountStructure(output);

            conflicts.UnionWith(FindNonInstructionSourceConflicts(inputRead, usableRows));
            conflicts.UnionWith(FindProtectedInstructionSourceConflicts(input, usableRows, classifier));
            usableRows = usableRows
                .Where(row => !conflicts.Contains(row.Source))
                .ToList();
            usableRowsCount = usableRows.Count;

            if (conflicts.Count > 0)
            {
                errors.Add($"Translation rows contain {conflicts.Count} conflicting or unsafe shared source(s).");
            }
            if (experimental)
            {
                errors.AddRange(ValidateExperimentalRows(input, fileName, game, candidateRows, classifier));
            }

            var expected = experimental
                ? ApplyExperimentalRows(input, fileName, usableRows, classifier)
                : ApplyRows(input, fileName, usableRows, dryRun: true, classifier);
            replacementCount = expected.Replacements.Count;
            if (expected.MissingRows.Count > 0)
            {
                errors.Add($"Translation rows contain {expected.MissingRows.Count} missing occurrence(s).");
            }
            if (inputStructure != outputStructure)
            {
                errors.Add("PEX structure counts changed between original and output.");
            }
            try
            {
                ValidateReparsedOutput(inputRead, outputRead, fileName, expected.Replacements, classifier);
            }
            catch (InvalidDataException ex)
            {
                errors.Add(ex.Message);
            }
        }
        catch (Exception ex)
        {
            errors.Add(ex.Message);
        }

        Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);
        WriteVerificationReport(
            reportPath,
            projectRoot,
            inputPex,
            translationJsonl,
            outputPex,
            game,
            category,
            capabilityLevel,
            rowsParsed,
            usableRowsCount,
            replacementCount,
            inputStructure,
            outputStructure,
            errors,
            classifier,
            inputCompatibility,
            outputCompatibility);

        Console.WriteLine($"PEX verification report: {reportPath}");
        Console.WriteLine($"Verification errors: {errors.Count}");
        return errors.Count == 0 ? 0 : 2;
    }

    private static int Apply(
        Options options,
        string game,
        GameCategory category,
        string capabilityLevel)
    {
        RequireWriteCapability(capabilityLevel);
        var projectRoot = FullPath(options.ProjectRoot ?? Directory.GetCurrentDirectory());
        var inputPex = FullPath(Require(options.InputPex, "--input-pex"));
        var translationJsonl = FullPath(Require(options.TranslationJsonl, "--translation-jsonl"));
        var outputPex = FullPath(Require(options.OutputPex, "--output-pex"));
        var reportPath = FullPath(Require(options.Report, "--report"));

        EnsureProjectPath(inputPex, projectRoot, "input PEX");
        EnsureProjectPath(translationJsonl, projectRoot, "translation JSONL");
        EnsureProjectPath(outputPex, projectRoot, "output PEX");
        EnsureProjectPath(reportPath, projectRoot, "report");
        EnsurePexOutputPath(outputPex, projectRoot);
        EnsurePexExtension(inputPex, "input PEX");
        EnsurePexExtension(outputPex, "output PEX");
        EnsureJsonlExtension(translationJsonl, "translation JSONL");
        EnsureMarkdownExtension(reportPath, "report");
        EnsureNoRiskyMarker(inputPex);
        EnsureNoRiskyMarker(translationJsonl);
        EnsureNoRiskyMarker(outputPex);
        EnsureNoRiskyMarker(reportPath);
        EnsureDistinctPaths(
            ("input PEX", inputPex),
            ("translation JSONL", translationJsonl),
            ("output PEX", outputPex),
            ("report", reportPath));

        var experimental = capabilityLevel == "experimental_write";
        if (experimental && !options.AllowExperimentalWriteback)
        {
            throw new InvalidOperationException(
                "PEX writeback capability is experimental; pass --allow-experimental-writeback for an explicit project-local attempt.");
        }
        DeleteIfExists(outputPex);
        DeleteIfExists(reportPath);

        var fileName = Path.GetFileName(inputPex);
        var classifier = PexCallSiteClassifier.ForGame(game, options.VisibleApiRegistry);
        var rows = ReadTranslationRows(translationJsonl, fileName, experimental);
        var candidateRows = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Where(row => !string.IsNullOrWhiteSpace(row.Target))
            .Where(row => !string.Equals(row.Source, row.Target, StringComparison.Ordinal))
            .ToList();
        var conflicts = FindConflicts(candidateRows);
        var usableRows = candidateRows
            .Where(row => !TranslationRowProtectsSource(row))
            .Where(row => !conflicts.Contains(row.Source))
            .ToList();

        var inputRead = PexCompatibilityReader.ReadFromFile(inputPex, category);
        var pex = inputRead.File;
        var inputStructure = CountStructure(pex);
        conflicts.UnionWith(FindNonInstructionSourceConflicts(inputRead, usableRows));
        conflicts.UnionWith(FindProtectedInstructionSourceConflicts(pex, usableRows, classifier));
        usableRows = usableRows
            .Where(row => !conflicts.Contains(row.Source))
            .ToList();
        var validationErrors = experimental
            ? ValidateExperimentalRows(pex, fileName, game, candidateRows, classifier)
            : [];
        var applyResult = experimental
            ? ApplyExperimentalRows(pex, fileName, usableRows, classifier)
            : ApplyRows(pex, fileName, usableRows, options.DryRun, classifier);

        var hasBlockingIssues = conflicts.Count > 0
            || validationErrors.Count > 0
            || applyResult.MissingRows.Count > 0;
        PexStructure? outputStructure = null;
        var structurePreserved = false;
        var outputPublished = false;
        PexCompatibilityMetadata? outputCompatibility = null;

        if (!options.DryRun && !hasBlockingIssues)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(outputPex)!);
            var tempOutput = Path.Combine(
                Path.GetDirectoryName(outputPex)!,
                $".{Path.GetFileName(outputPex)}.{Guid.NewGuid():N}.tmp");
            try
            {
                PatchPexStringTable(inputPex, tempOutput, usableRows, category);
                var outputRead = PexCompatibilityReader.ReadFromFile(tempOutput, category);
                var reparsed = outputRead.File;
                outputCompatibility = outputRead.Compatibility;
                outputStructure = CountStructure(reparsed);
                structurePreserved = inputStructure == outputStructure;
                if (!structurePreserved)
                {
                    throw new InvalidDataException("PEX structure counts changed after writeback.");
                }
                ValidateReparsedOutput(inputRead, outputRead, fileName, applyResult.Replacements, classifier);
                File.Move(tempOutput, outputPex);
                outputPublished = true;
            }
            catch
            {
                DeleteIfExists(tempOutput);
                DeleteIfExists(outputPex);
                throw;
            }
        }

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);
            WriteApplyReport(
                reportPath,
                projectRoot,
                inputPex,
                translationJsonl,
                outputPex,
                options.DryRun,
                rows,
                usableRows,
                conflicts,
                validationErrors,
                applyResult,
                game,
                category,
                capabilityLevel,
                experimental,
                options.AllowExperimentalWriteback,
                inputStructure,
                outputStructure,
                structurePreserved,
                outputPublished,
                classifier,
                inputRead.Compatibility,
                outputCompatibility);
        }
        catch
        {
            if (outputPublished)
            {
                DeleteIfExists(outputPex);
            }
            DeleteIfExists(reportPath);
            throw;
        }

        Console.WriteLine($"PEX apply report: {reportPath}");
        Console.WriteLine($"Rows parsed: {rows.Count}");
        Console.WriteLine($"Usable rows: {usableRows.Count}");
        Console.WriteLine($"Instruction string replacements: {applyResult.Replacements.Count}");

        if (hasBlockingIssues)
        {
            return 2;
        }
        return 0;
    }

    private static void PatchPexStringTable(
        string inputPex,
        string outputPex,
        IReadOnlyCollection<TranslationRow> usableRows,
        GameCategory category)
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

        var bigEndian = category == GameCategory.Skyrim;
        SkipPexHeader(reader, bigEndian);
        var stringCountPosition = checked((int)input.Position);
        var stringCount = ReadUInt16(reader, bigEndian);
        output.Write(inputBytes, 0, stringCountPosition);
        WriteUInt16(writer, stringCount, bigEndian);

        var cp1252 = Encoding.GetEncoding(1252);
        for (var i = 0; i < stringCount; i++)
        {
            var length = ReadUInt16(reader, bigEndian);
            var bytes = reader.ReadBytes(length);
            if (bytes.Length != length)
            {
                throw new InvalidDataException($"PEX string table ended unexpectedly at index {i}.");
            }

            var utf8Text = Encoding.UTF8.GetString(bytes);
            var cp1252Text = cp1252.GetString(bytes);
            if (replacements.TryGetValue(RepairUtf8Mojibake(utf8Text), out var target)
                || replacements.TryGetValue(RepairUtf8Mojibake(cp1252Text), out target))
            {
                var targetBytes = Encoding.UTF8.GetBytes(target);
                if (targetBytes.Length > ushort.MaxValue)
                {
                    throw new InvalidDataException($"Replacement string is too long for PEX string table: {utf8Text}");
                }
                WriteUInt16(writer, (ushort)targetBytes.Length, bigEndian);
                writer.Write(targetBytes);
                continue;
            }

            WriteUInt16(writer, length, bigEndian);
            writer.Write(bytes);
        }

        var restOffset = checked((int)input.Position);
        output.Write(inputBytes, restOffset, inputBytes.Length - restOffset);

        Directory.CreateDirectory(Path.GetDirectoryName(outputPex)!);
        File.WriteAllBytes(outputPex, output.ToArray());
    }

    private static void SkipPexHeader(BinaryReader reader, bool bigEndian)
    {
        const uint pexMagic = 0xFA57C0DE;
        var magic = ReadUInt32(reader, bigEndian);
        if (magic != pexMagic)
        {
            throw new InvalidDataException($"File does not have fast code! Magic does not match {pexMagic:x8} is {magic:x8}");
        }

        reader.ReadByte();
        reader.ReadByte();
        ReadUInt16(reader, bigEndian);
        ReadUInt64(reader, bigEndian);
        SkipPrependedString(reader, bigEndian);
        SkipPrependedString(reader, bigEndian);
        SkipPrependedString(reader, bigEndian);
    }

    private static void SkipPrependedString(BinaryReader reader, bool bigEndian)
    {
        var length = ReadUInt16(reader, bigEndian);
        var bytes = reader.ReadBytes(length);
        if (bytes.Length != length)
        {
            throw new InvalidDataException("PEX header string ended unexpectedly.");
        }
    }

    private static ushort ReadUInt16(BinaryReader reader, bool bigEndian)
    {
        var bytes = reader.ReadBytes(sizeof(ushort));
        if (bytes.Length != sizeof(ushort))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt16.");
        }
        return bigEndian
            ? BinaryPrimitives.ReadUInt16BigEndian(bytes)
            : BinaryPrimitives.ReadUInt16LittleEndian(bytes);
    }

    private static uint ReadUInt32(BinaryReader reader, bool bigEndian)
    {
        var bytes = reader.ReadBytes(sizeof(uint));
        if (bytes.Length != sizeof(uint))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt32.");
        }
        return bigEndian
            ? BinaryPrimitives.ReadUInt32BigEndian(bytes)
            : BinaryPrimitives.ReadUInt32LittleEndian(bytes);
    }

    private static ulong ReadUInt64(BinaryReader reader, bool bigEndian)
    {
        var bytes = reader.ReadBytes(sizeof(ulong));
        if (bytes.Length != sizeof(ulong))
        {
            throw new InvalidDataException("Unexpected end of PEX stream while reading UInt64.");
        }
        return bigEndian
            ? BinaryPrimitives.ReadUInt64BigEndian(bytes)
            : BinaryPrimitives.ReadUInt64LittleEndian(bytes);
    }

    private static void WriteUInt16(BinaryWriter writer, ushort value, bool bigEndian)
    {
        Span<byte> bytes = stackalloc byte[sizeof(ushort)];
        if (bigEndian)
        {
            BinaryPrimitives.WriteUInt16BigEndian(bytes, value);
        }
        else
        {
            BinaryPrimitives.WriteUInt16LittleEndian(bytes, value);
        }
        writer.Write(bytes);
    }

    private static ApplyResult ApplyRows(
        PexFile pex,
        string fileName,
        List<TranslationRow> rows,
        bool dryRun,
        PexCallSiteClassifier classifier)
    {
        var result = new ApplyResult();
        var exactRows = rows
            .GroupBy(row => row.Source, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.Ordinal);
        var ignoreCaseRows = rows
            .Where(row => row.IgnoreCase)
            .GroupBy(row => row.Source, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.OrdinalIgnoreCase);

        foreach (var occurrence in EnumerateInstructionStrings(pex, fileName, classifier))
        {
            if (string.IsNullOrEmpty(occurrence.Text))
            {
                continue;
            }

            var source = occurrence.Text;
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
                occurrence.Callee,
                occurrence.SemanticArgumentIndex,
                occurrence.SemanticArgumentRole,
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

    private static ApplyResult ApplyExperimentalRows(
        PexFile pex,
        string fileName,
        List<TranslationRow> rows,
        PexCallSiteClassifier classifier)
    {
        var result = new ApplyResult();
        var rowsByIdentity = rows
            .GroupBy(TranslationIdentity, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.Ordinal);

        foreach (var occurrence in EnumerateInstructionStrings(pex, fileName, classifier))
        {
            if (!rowsByIdentity.TryGetValue(OccurrenceIdentity(occurrence), out var row)
                || !string.Equals(occurrence.Text, row.Source, StringComparison.Ordinal))
            {
                continue;
            }

            result.FoundRowLines.Add(row.LineNumber);
            result.FoundSources.Add(row.Source);
            result.Replacements.Add(new Replacement(
                occurrence.ObjectName,
                occurrence.StateName,
                occurrence.FunctionName,
                occurrence.OpCode,
                occurrence.InstructionIndex,
                occurrence.ArgumentIndex,
                occurrence.Callee,
                occurrence.SemanticArgumentIndex,
                occurrence.SemanticArgumentRole,
                occurrence.Text,
                row.Target));
        }

        foreach (var row in rows)
        {
            if (!result.FoundRowLines.Contains(row.LineNumber))
            {
                result.MissingRows.Add(row);
            }
        }
        return result;
    }

    private static List<string> ValidateExperimentalRows(
        PexFile pex,
        string fileName,
        string game,
        List<TranslationRow> rows,
        PexCallSiteClassifier classifier)
    {
        var errors = new List<string>();
        if (rows.Count == 0)
        {
            errors.Add("Experimental PEX writeback requires at least one writable schema v2 row.");
        }
        var occurrences = EnumerateInstructionStrings(pex, fileName, classifier).ToList();
        var actualByIdentity = occurrences.ToDictionary(OccurrenceIdentity, StringComparer.Ordinal);
        var actualByStructuralIdentity = occurrences
            .GroupBy(OccurrenceStructuralIdentity, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.ToList(), StringComparer.Ordinal);
        var acceptedRows = new List<TranslationRow>();
        var seenIdentities = new HashSet<string>(StringComparer.Ordinal);

        foreach (var row in rows)
        {
            var valid = true;
            if (row.SchemaVersion != 2)
            {
                errors.Add($"line {row.LineNumber}: Fallout 4 PEX writeback requires schema_version=2.");
                valid = false;
            }
            if (!string.Equals(row.GameId, game, StringComparison.Ordinal))
            {
                errors.Add($"line {row.LineNumber}: game_id must be {game}, found '{row.GameId}'.");
                valid = false;
            }
            if (!string.Equals(row.ModName, fileName, StringComparison.OrdinalIgnoreCase))
            {
                errors.Add($"line {row.LineNumber}: file identity must be {fileName}, found '{row.ModName}'.");
                valid = false;
            }
            if (row.IgnoreCase)
            {
                errors.Add($"line {row.LineNumber}: ignore-case matching is not allowed for experimental PEX writeback.");
                valid = false;
            }
            if (string.IsNullOrWhiteSpace(row.ObjectName)
                || string.IsNullOrWhiteSpace(row.FunctionName)
                || string.IsNullOrWhiteSpace(row.OpCode)
                || row.InstructionIndex < 0
                || row.ArgumentIndex < 0)
            {
                errors.Add($"line {row.LineNumber}: exact occurrence identity is incomplete.");
                valid = false;
            }
            if (!string.Equals(row.Classification, "visible", StringComparison.Ordinal)
                || !row.IsDirectLiteral
                || string.IsNullOrWhiteSpace(row.Callee)
                || row.SemanticArgumentIndex < 0
                || string.IsNullOrWhiteSpace(row.SemanticArgumentRole)
                || string.IsNullOrWhiteSpace(row.VisibilityBasis))
            {
                errors.Add(
                    $"line {row.LineNumber}: Fallout 4 PEX writeback requires a direct visible literal authorized by the active API registry.");
                valid = false;
            }
            if (!string.IsNullOrWhiteSpace(row.OpcodeForm)
                && !string.Equals(row.OpcodeForm, row.OpCode, StringComparison.Ordinal))
            {
                errors.Add($"line {row.LineNumber}: opcode_form does not match opcode.");
                valid = false;
            }
            if (TranslationRowProtectsSource(row))
            {
                errors.Add($"line {row.LineNumber}: protected metadata or comparison occurrence cannot be authorized for writeback.");
                valid = false;
            }

            var identity = TranslationIdentity(row);
            if (!seenIdentities.Add(identity))
            {
                errors.Add($"line {row.LineNumber}: duplicate occurrence identity.");
                valid = false;
            }
            if (!actualByIdentity.TryGetValue(identity, out var occurrence))
            {
                if (actualByStructuralIdentity.ContainsKey(TranslationStructuralIdentity(row)))
                {
                    errors.Add($"line {row.LineNumber}: semantic source drift changed the callee or argument role.");
                }
                else
                {
                    errors.Add($"line {row.LineNumber}: occurrence identity no longer exists in the input PEX.");
                }
                valid = false;
            }
            else if (!string.Equals(row.Source, occurrence.Text, StringComparison.Ordinal))
            {
                errors.Add($"line {row.LineNumber}: source text drifted from the exported occurrence.");
                valid = false;
            }
            else if (!occurrence.IsDirectLiteral
                     || !string.Equals(occurrence.Classification, "visible", StringComparison.Ordinal)
                     || !string.Equals(row.VisibilityBasis, occurrence.VisibilityBasis, StringComparison.Ordinal))
            {
                errors.Add($"line {row.LineNumber}: active PEX semantic authorization no longer matches the export row.");
                valid = false;
            }

            if (valid)
            {
                acceptedRows.Add(row);
            }
        }

        foreach (var group in acceptedRows.GroupBy(row => row.Source, StringComparer.Ordinal))
        {
            var actualIdentities = occurrences
                .Where(occurrence => string.Equals(occurrence.Text, group.Key, StringComparison.Ordinal))
                .Select(OccurrenceIdentity)
                .ToHashSet(StringComparer.Ordinal);
            var authorizedIdentities = group
                .Select(TranslationIdentity)
                .ToHashSet(StringComparer.Ordinal);
            if (!actualIdentities.SetEquals(authorizedIdentities))
            {
                errors.Add(
                    $"source '{group.Key}' is shared by {actualIdentities.Count} occurrence(s), but "
                    + $"{authorizedIdentities.Count} exact occurrence(s) were authorized.");
            }
            if (group.Select(row => row.Target).Distinct(StringComparer.Ordinal).Count() != 1)
            {
                errors.Add($"source '{group.Key}' has conflicting target translations.");
            }
        }
        return errors;
    }

    private static string OccurrenceIdentity(PexStringOccurrence occurrence)
    {
        return string.Join(
            '\u001F',
            occurrence.FileName,
            occurrence.ObjectName,
            occurrence.StateName,
            occurrence.FunctionName,
            occurrence.OpCode,
            occurrence.InstructionIndex,
            occurrence.ArgumentIndex,
            occurrence.Callee,
            occurrence.SemanticArgumentIndex,
            occurrence.SemanticArgumentRole);
    }

    private static string TranslationIdentity(TranslationRow row)
    {
        return string.Join(
            '\u001F',
            row.ModName,
            row.ObjectName,
            row.StateName,
            row.FunctionName,
            row.OpCode,
            row.InstructionIndex,
            row.ArgumentIndex,
            row.Callee,
            row.SemanticArgumentIndex,
            row.SemanticArgumentRole);
    }

    private static string OccurrenceStructuralIdentity(PexStringOccurrence occurrence)
    {
        return string.Join(
            '\u001F',
            occurrence.FileName,
            occurrence.ObjectName,
            occurrence.StateName,
            occurrence.FunctionName,
            occurrence.OpCode,
            occurrence.InstructionIndex,
            occurrence.ArgumentIndex);
    }

    private static string TranslationStructuralIdentity(TranslationRow row)
    {
        return string.Join(
            '\u001F',
            row.ModName,
            row.ObjectName,
            row.StateName,
            row.FunctionName,
            row.OpCode,
            row.InstructionIndex,
            row.ArgumentIndex);
    }

    private static PexStructure CountStructure(PexFile pex)
    {
        var functions = EnumerateFunctions(pex).ToList();
        return new PexStructure(
            pex.Objects.Count,
            pex.Objects.Sum(item => item.States.Count),
            functions.Count,
            functions.Sum(item => item.Instructions.Count));
    }

    private static IEnumerable<PexObjectFunction> EnumerateFunctions(PexFile pex)
    {
        foreach (var pexObject in pex.Objects)
        {
            foreach (var property in pexObject.Properties)
            {
                if (property.ReadHandler is not null)
                {
                    yield return property.ReadHandler;
                }
                if (property.WriteHandler is not null)
                {
                    yield return property.WriteHandler;
                }
            }
            foreach (var state in pexObject.States)
            {
                foreach (var namedFunction in state.Functions)
                {
                    if (namedFunction.Function is not null)
                    {
                        yield return namedFunction.Function;
                    }
                }
            }
        }
    }

    private static string CreatePexInvariantSnapshot(PexReadResult readResult)
    {
        var pex = readResult.File;
        var snapshot = new
        {
            Header = new
            {
                pex.MajorVersion,
                pex.MinorVersion,
                pex.GameId,
                CompilationTimeTicks = pex.CompilationTime.Ticks,
                CompilationTimeKind = (int)pex.CompilationTime.Kind,
                pex.SourceFileName,
                pex.Username,
                pex.MachineName,
                UserFlags = pex.UserFlags.ToArray(),
            },
            DebugInfo = pex.DebugInfo is null
                ? null
                : new
                {
                    ModificationTimeTicks = pex.DebugInfo.ModificationTime.Ticks,
                    ModificationTimeKind = (int)pex.DebugInfo.ModificationTime.Kind,
                    Functions = pex.DebugInfo.Functions.Select(function => new
                    {
                        function.ObjectName,
                        function.StateName,
                        function.FunctionName,
                        FunctionType = (int)function.FunctionType,
                        Instructions = function.Instructions.ToArray(),
                    }).ToArray(),
                    PropertyGroups = pex.DebugInfo.PropertyGroups.Select(group => new
                    {
                        group.ObjectName,
                        group.GroupName,
                        PropertyNames = group.PropertyNames.ToArray(),
                    }).ToArray(),
                    StructOrders = pex.DebugInfo.StructOrders.Select(order => new
                    {
                        order.ObjectName,
                        order.OrderName,
                        Names = order.Names.ToArray(),
                    }).ToArray(),
                },
            Compatibility = new
            {
                readResult.Compatibility.Layout,
                readResult.Compatibility.StringCount,
                readResult.Compatibility.PostStringTablePayloadSha256,
                DebugPropertyGroups = readResult.Compatibility.DebugPropertyGroups.Select(group => new
                {
                    group.ObjectName,
                    group.GroupName,
                    group.DocString,
                    group.UserFlags,
                    PropertyNames = group.PropertyNames.ToArray(),
                }).ToArray(),
            },
            Objects = pex.Objects.Select(pexObject => new
            {
                pexObject.Name,
                pexObject.ParentClassName,
                pexObject.DocString,
                pexObject.AutoStateName,
                pexObject.RawUserFlags,
                pexObject.IsConst,
                StructInfos = pexObject.StructInfos.Select(structInfo => new
                {
                    structInfo.Name,
                    Members = structInfo.Members.Select(member => new
                    {
                        member.Name,
                        member.TypeName,
                        member.DocString,
                        member.RawUserFlags,
                        member.IsConst,
                        Value = SnapshotVariableData(member.Value, omitInstructionString: false),
                    }).ToArray(),
                }).ToArray(),
                Variables = pexObject.Variables.Select(variable => new
                {
                    variable.Name,
                    variable.TypeName,
                    variable.RawUserFlags,
                    Value = SnapshotVariableData(variable.VariableData, omitInstructionString: false),
                }).ToArray(),
                Properties = pexObject.Properties.Select(property => new
                {
                    property.Name,
                    property.TypeName,
                    property.DocString,
                    property.AutoVarName,
                    property.RawUserFlags,
                    Flags = (int)property.Flags,
                    ReadHandler = property.ReadHandler is null ? null : SnapshotFunction(property.ReadHandler),
                    WriteHandler = property.WriteHandler is null ? null : SnapshotFunction(property.WriteHandler),
                }).ToArray(),
                States = pexObject.States.Select(state => new
                {
                    state.Name,
                    Functions = state.Functions.Select(function => new
                    {
                        function.FunctionName,
                        Function = function.Function is null ? null : SnapshotFunction(function.Function),
                    }).ToArray(),
                }).ToArray(),
            }).ToArray(),
        };
        return JsonSerializer.Serialize(snapshot);
    }

    private static object SnapshotFunction(PexObjectFunction function)
    {
        return new
        {
            function.ReturnTypeName,
            function.DocString,
            function.RawUserFlags,
            Flags = (int)function.Flags,
            Parameters = function.Parameters.Select(parameter => new
            {
                parameter.Name,
                parameter.TypeName,
            }).ToArray(),
            Locals = function.Locals.Select(local => new
            {
                local.Name,
                local.TypeName,
            }).ToArray(),
            Instructions = function.Instructions.Select(instruction => new
            {
                OpCode = (int)instruction.OpCode,
                Arguments = instruction.Arguments.Select(argument =>
                    SnapshotVariableData(argument, omitInstructionString: true)).ToArray(),
            }).ToArray(),
        };
    }

    private static object? SnapshotVariableData(
        PexObjectVariableData? value,
        bool omitInstructionString)
    {
        if (value is null)
        {
            return null;
        }
        return new
        {
            VariableType = (int)value.VariableType,
            value.BoolValue,
            value.IntValue,
            FloatBits = value.FloatValue.HasValue
                ? BitConverter.SingleToInt32Bits(value.FloatValue.Value)
                : (int?)null,
            StringValue = omitInstructionString && value.VariableType == VariableType.String
                ? null
                : value.StringValue,
        };
    }

    private static void ValidateReparsedOutput(
        PexReadResult inputRead,
        PexReadResult outputRead,
        string fileName,
        IReadOnlyCollection<Replacement> replacements,
        PexCallSiteClassifier classifier)
    {
        var input = inputRead.File;
        var output = outputRead.File;
        var inputInvariantSnapshot = CreatePexInvariantSnapshot(inputRead);
        var outputInvariantSnapshot = CreatePexInvariantSnapshot(outputRead);
        if (!string.Equals(inputInvariantSnapshot, outputInvariantSnapshot, StringComparison.Ordinal))
        {
            throw new InvalidDataException("PEX invariant metadata changed after writeback.");
        }

        var inputOccurrences = EnumerateInstructionStrings(input, fileName, classifier)
            .ToDictionary(OccurrenceIdentity, StringComparer.Ordinal);
        var outputOccurrences = EnumerateInstructionStrings(output, fileName, classifier)
            .ToDictionary(OccurrenceIdentity, StringComparer.Ordinal);
        if (!inputOccurrences.Keys.ToHashSet(StringComparer.Ordinal).SetEquals(outputOccurrences.Keys))
        {
            throw new InvalidDataException("PEX occurrence identities changed after writeback.");
        }

        var expectedTargets = replacements.ToDictionary(
            item => string.Join(
                '\u001F',
                fileName,
                item.ObjectName,
                item.StateName,
                item.FunctionName,
                item.OpCode,
                item.InstructionIndex,
                item.ArgumentIndex,
                item.Callee,
                item.SemanticArgumentIndex,
                item.SemanticArgumentRole),
            item => item.Target,
            StringComparer.Ordinal);
        foreach (var pair in inputOccurrences)
        {
            var expected = expectedTargets.TryGetValue(pair.Key, out var target)
                ? target
                : pair.Value.Text;
            if (!string.Equals(outputOccurrences[pair.Key].Text, expected, StringComparison.Ordinal))
            {
                throw new InvalidDataException($"Unexpected PEX string change at occurrence {pair.Key}.");
            }
        }

        var inputMetadata = EnumerateNonInstructionStrings(input).ToList();
        var outputMetadata = EnumerateNonInstructionStrings(output).ToList();
        if (!inputMetadata.SequenceEqual(outputMetadata, StringComparer.Ordinal))
        {
            throw new InvalidDataException("PEX metadata strings changed after writeback.");
        }
    }

    private static void DeleteIfExists(string path)
    {
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }

    private static IEnumerable<PexStringOccurrence> EnumerateInstructionStrings(
        PexFile pex,
        string fileName,
        PexCallSiteClassifier classifier)
    {
        foreach (var pexObject in pex.Objects)
        {
            foreach (var property in pexObject.Properties)
            {
                if (property.ReadHandler is not null)
                {
                    foreach (var occurrence in EnumerateFunctionStrings(
                                 fileName,
                                 pexObject.Name ?? "",
                                 pexObject.ParentClassName ?? "",
                                 "",
                                 $"{property.Name}.get",
                                 property.ReadHandler,
                                 classifier))
                    {
                        yield return occurrence;
                    }
                }
                if (property.WriteHandler is not null)
                {
                    foreach (var occurrence in EnumerateFunctionStrings(
                                 fileName,
                                 pexObject.Name ?? "",
                                 pexObject.ParentClassName ?? "",
                                 "",
                                 $"{property.Name}.set",
                                 property.WriteHandler,
                                 classifier))
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
                                 pexObject.ParentClassName ?? "",
                                 state.Name ?? "",
                                 namedFunction.FunctionName ?? "",
                                 namedFunction.Function,
                                 classifier))
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
        string parentClassName,
        string stateName,
        string functionName,
        PexObjectFunction function,
        PexCallSiteClassifier classifier)
    {
        for (var instructionIndex = 0; instructionIndex < function.Instructions.Count; instructionIndex++)
        {
            var instruction = function.Instructions[instructionIndex];
            foreach (var classified in classifier.ClassifyInstruction(instruction, parentClassName))
            {
                yield return new PexStringOccurrence(
                    fileName,
                    objectName,
                    stateName,
                    functionName,
                    instruction.OpCode.ToString(),
                    instructionIndex,
                    classified.RawArgumentIndex,
                    classified.SemanticArgumentIndex,
                    classified.Argument,
                    classified.IsDirectLiteral,
                    classified.Callee,
                    classified.SemanticArgumentRole,
                    classified.VisibilityBasis,
                    classified.Classification);
            }
        }
    }

    private static List<TranslationRow> ReadTranslationRows(
        string translationJsonl,
        string fileName,
        bool requireExactIdentity)
    {
        var rows = new List<TranslationRow>();
        var lineNumber = 0;
        foreach (var line in File.ReadLines(translationJsonl, StrictUtf8))
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
                && !string.Equals(modName, fileName, StringComparison.OrdinalIgnoreCase)
                && !requireExactIdentity)
            {
                continue;
            }

            var schemaVersion = GetInt(root, "schema_version", "SchemaVersion");
            var gameId = GetString(root, "game_id", "GameId");
            var source = DecodeControlEscapes(GetString(root, "Source", "source", "original", "text"));
            var target = DecodeControlEscapes(GetString(root, "Result", "result", "Target", "target", "translation"));
            var ignoreCase = GetBool(root, "IgnoreCase", "ignore_case");
            var risk = GetString(root, "risk", "Risk");
            var opcode = GetString(root, "opcode", "Opcode", "op", "Op");
            var objectName = GetString(root, "object_name", "ObjectName");
            var stateName = GetString(root, "state_name", "StateName");
            var functionName = GetString(root, "function_name", "FunctionName");
            var instructionIndex = GetInt(root, "instruction_index", "InstructionIndex", fallback: -1);
            var argumentIndex = GetInt(root, "argument_index", "ArgumentIndex", fallback: -1);
            var opcodeForm = GetString(root, "opcode_form", "OpcodeForm");
            var callee = GetString(root, "callee", "Callee");
            var semanticArgumentIndex = GetInt(
                root,
                "semantic_argument_index",
                "SemanticArgumentIndex",
                fallback: -1);
            var semanticArgumentRole = GetString(
                root,
                "semantic_argument_role",
                "SemanticArgumentRole");
            var visibilityBasis = GetString(root, "visibility_basis", "VisibilityBasis");
            var classification = GetString(root, "classification", "Classification");
            var valueKind = GetString(root, "value_kind", "ValueKind");
            var isDirectLiteral = GetBool(root, "is_direct_literal", "IsDirectLiteral");
            rows.Add(new TranslationRow(
                lineNumber,
                schemaVersion,
                gameId,
                modName,
                objectName,
                stateName,
                functionName,
                opcode,
                opcodeForm,
                instructionIndex,
                argumentIndex,
                callee,
                semanticArgumentIndex,
                semanticArgumentRole,
                visibilityBasis,
                classification,
                valueKind,
                isDirectLiteral,
                source,
                target,
                ignoreCase,
                risk));
        }
        return rows;
    }

    private static bool TranslationRowProtectsSource(TranslationRow row)
    {
        var risk = row.Risk.Trim().ToLowerInvariant();
        var opcode = row.OpCode.Trim().ToUpperInvariant();
        var classification = row.Classification.Trim().ToLowerInvariant();
        return classification is "protected" or "manual_review"
            || (!string.IsNullOrWhiteSpace(classification) && !row.IsDirectLiteral)
            || risk is "blocked" or "logic" or "manual" or "protected" or "protected-logic" or "review" or "unsafe"
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

    private static HashSet<string> FindNonInstructionSourceConflicts(
        PexReadResult readResult,
        IEnumerable<TranslationRow> rows)
    {
        var sources = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Select(row => row.Source!)
            .ToHashSet(StringComparer.Ordinal);
        if (sources.Count == 0)
        {
            return [];
        }

        var metadataStrings = EnumerateNonInstructionStrings(readResult.File)
            .Concat(readResult.Compatibility.DebugPropertyGroups.Select(group => group.DocString))
            .Where(value => !string.IsNullOrEmpty(value))
            .Select(RepairUtf8Mojibake)
            .ToHashSet(StringComparer.Ordinal);
        sources.IntersectWith(metadataStrings);
        return sources;
    }

    private static HashSet<string> FindProtectedInstructionSourceConflicts(
        PexFile pex,
        IEnumerable<TranslationRow> rows,
        PexCallSiteClassifier classifier)
    {
        var sources = rows
            .Where(row => !string.IsNullOrWhiteSpace(row.Source))
            .Select(row => row.Source!)
            .ToHashSet(StringComparer.Ordinal);
        if (sources.Count == 0)
        {
            return [];
        }

        var protectedSources = EnumerateInstructionStrings(pex, "", classifier)
            .Where(occurrence => occurrence.OpCode.StartsWith("CMP_", StringComparison.Ordinal)
                                 || (classifier.UsesSemanticRegistry
                                     && !string.Equals(occurrence.Classification, "visible", StringComparison.Ordinal)))
            .Select(occurrence => occurrence.Text)
            .ToHashSet(StringComparer.Ordinal);
        sources.IntersectWith(protectedSources);
        return sources;
    }

    private static IEnumerable<string> EnumerateNonInstructionStrings(PexFile pex)
    {
        foreach (var function in EnumerateFunctions(pex))
        {
            foreach (var instruction in function.Instructions)
            {
                foreach (var argument in instruction.Arguments)
                {
                    if (argument.VariableType != VariableType.String
                        && !string.IsNullOrEmpty(argument.StringValue))
                    {
                        yield return argument.StringValue;
                    }
                }
            }
        }

        if (pex.DebugInfo is not null)
        {
            foreach (var function in pex.DebugInfo.Functions)
            {
                foreach (var value in Strings(function.ObjectName, function.StateName, function.FunctionName))
                {
                    yield return value;
                }
            }
            foreach (var propertyGroup in pex.DebugInfo.PropertyGroups)
            {
                foreach (var value in Strings(propertyGroup.ObjectName, propertyGroup.GroupName))
                {
                    yield return value;
                }
                foreach (var propertyName in propertyGroup.PropertyNames)
                {
                    if (!string.IsNullOrEmpty(propertyName))
                    {
                        yield return propertyName;
                    }
                }
            }
            foreach (var structOrder in pex.DebugInfo.StructOrders)
            {
                foreach (var value in Strings(structOrder.ObjectName, structOrder.OrderName))
                {
                    yield return value;
                }
                foreach (var name in structOrder.Names)
                {
                    if (!string.IsNullOrEmpty(name))
                    {
                        yield return name;
                    }
                }
            }
        }

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
                if (property.ReadHandler is not null)
                {
                    foreach (var value in EnumerateFunctionMetadataStrings(property.ReadHandler))
                    {
                        yield return value;
                    }
                }
                if (property.WriteHandler is not null)
                {
                    foreach (var value in EnumerateFunctionMetadataStrings(property.WriteHandler))
                    {
                        yield return value;
                    }
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

    private static IEnumerable<string> EnumerateFunctionMetadataStrings(PexObjectFunction function)
    {
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

    private static int GetInt(JsonElement root, string name, string alias, int fallback = 0)
    {
        foreach (var key in new[] { name, alias })
        {
            if (!root.TryGetProperty(key, out var value))
            {
                continue;
            }
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number))
            {
                return number;
            }
            if (value.ValueKind == JsonValueKind.String && int.TryParse(value.GetString(), out var parsed))
            {
                return parsed;
            }
        }
        return fallback;
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

    private static void WriteExportReport(
        string reportPath,
        string projectRoot,
        string inputPex,
        string outputJsonl,
        List<PexStringOccurrence> occurrences,
        string game,
        GameCategory category,
        string capabilityLevel,
        PexCallSiteClassifier classifier,
        PexCompatibilityMetadata compatibility)
    {
        var unique = occurrences.Select(item => item.Text).Distinct(StringComparer.Ordinal).Count();
        var candidate = occurrences.Count(item => RiskForOccurrence(item) == "candidate");
        var manualReview = occurrences.Count(item => RiskForOccurrence(item) == "manual-review");
        var protectedLogic = occurrences.Count(item => RiskForOccurrence(item) == "protected-logic");

        var lines = new List<string>
        {
            "# Mutagen PEX String Export Report",
            "",
            $"- game_id: {game}",
            $"- pex_category: {category}",
            $"- capability_level: {capabilityLevel}",
            $"- semantic_registry: {(classifier.UsesSemanticRegistry ? Path.GetFileName(classifier.RegistryPath) : "not-used")}",
            $"- semantic_registry_sha256: {classifier.RegistrySha256}",
            $"- pex_layout: {compatibility.Layout}",
            $"- official_fallout4_layout_normalized: {compatibility.NormalizedOfficialFallout4Layout}",
            $"- post_string_table_payload_sha256: {compatibility.PostStringTablePayloadSha256}",
            "- schema_version: 2",
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
            "- Exported direct instruction string literals and manual-review evidence for registered visible arguments supplied dynamically.",
            "- Did not treat function names, variable names, property names, state names, user flags, or debug symbols as writable text.",
            "- This is a read-only export; no PEX was modified.",
            "",
            "## Safety",
            "",
            "- All paths were checked to be inside the project root.",
            "- No real Skyrim/Fallout 4, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.",
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
        List<string> validationErrors,
        ApplyResult result,
        string game,
        GameCategory category,
        string capabilityLevel,
        bool experimental,
        bool experimentalOptIn,
        PexStructure inputStructure,
        PexStructure? outputStructure,
        bool structurePreserved,
        bool outputPublished,
        PexCallSiteClassifier classifier,
        PexCompatibilityMetadata inputCompatibility,
        PexCompatibilityMetadata? outputCompatibility)
    {
        var lines = new List<string>
        {
            "# Mutagen PEX String Tool Report",
            "",
            $"- game_id: {game}",
            $"- pex_category: {category}",
            $"- capability_level: {capabilityLevel}",
            $"- semantic_registry: {(classifier.UsesSemanticRegistry ? Path.GetFileName(classifier.RegistryPath) : "not-used")}",
            $"- semantic_registry_sha256: {classifier.RegistrySha256}",
            $"- input_pex_layout: {inputCompatibility.Layout}",
            $"- output_pex_layout: {outputCompatibility?.Layout ?? ""}",
            $"- input_post_string_table_payload_sha256: {inputCompatibility.PostStringTablePayloadSha256}",
            $"- output_post_string_table_payload_sha256: {outputCompatibility?.PostStringTablePayloadSha256 ?? ""}",
            $"- writeback_status: {(experimental ? "experimental" : "stable")}",
            $"- experimental_opt_in: {experimentalOptIn}",
            $"- Input PEX: {Relative(projectRoot, inputPex)}",
            $"- Translation JSONL: {Relative(projectRoot, translationJsonl)}",
            $"- Output PEX: {Relative(projectRoot, outputPex)}",
            $"- Input SHA256: {FileSha256(inputPex)}",
            $"- Translation JSONL SHA256: {FileSha256(translationJsonl)}",
            $"- Output SHA256: {(outputPublished && File.Exists(outputPex) ? FileSha256(outputPex) : "")}",
            $"- Checked at: {DateTime.Now:yyyy-MM-dd HH:mm:ss}",
            $"- Dry run: {dryRun}",
            $"- Rows parsed for this PEX: {rows.Count}",
            $"- Usable rows: {usableRows.Count}",
            $"- Conflicting source rows: {conflicts.Count}",
            $"- Validation errors: {validationErrors.Count}",
            $"- Instruction string replacements: {result.Replacements.Count}",
            $"- Missing usable rows: {result.MissingRows.Count}",
            $"- Input objects: {inputStructure.Objects}",
            $"- Output objects: {outputStructure?.Objects.ToString() ?? ""}",
            $"- Input states: {inputStructure.States}",
            $"- Output states: {outputStructure?.States.ToString() ?? ""}",
            $"- Input functions: {inputStructure.Functions}",
            $"- Output functions: {outputStructure?.Functions.ToString() ?? ""}",
            $"- Input instructions: {inputStructure.Instructions}",
            $"- Output instructions: {outputStructure?.Instructions.ToString() ?? ""}",
            $"- Structure preserved: {structurePreserved}",
            $"- Output published: {outputPublished}",
            "",
            "## Replacements",
            "",
        };
        lines.AddRange(result.Replacements.Count == 0
            ? ["No replacements."]
            : result.Replacements.Select(item => $"- {item.ObjectName}.{item.FunctionName} [{item.OpCode} #{item.InstructionIndex}:{item.ArgumentIndex} {item.Callee}/{item.SemanticArgumentRole}]: `{EscapeInline(item.Source)}` -> `{EscapeInline(item.Target)}`"));
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
        lines.Add("## Exact Identity Validation");
        lines.Add("");
        lines.AddRange(validationErrors.Count == 0
            ? ["No exact identity validation errors."]
            : validationErrors.Select(item => $"- {item}"));
        lines.Add("");
        lines.Add("## Scope");
        lines.Add("");
        lines.Add("- Applied only to source strings first found in `VariableType.String` arguments inside PEX function instructions.");
        lines.Add("- Skipped any source string also found in non-instruction metadata or protected comparison instructions, to avoid globally changing names, identifiers, user flags, source file names, debug symbols, or logic comparisons.");
        lines.Add("- Patched only the PEX global string table, then re-read the output PEX to confirm it remains parseable.");
        if (experimental)
        {
            lines.Add("- Experimental writeback required schema v2 exact visible call-site authorization for every reference sharing a source string.");
        }
        lines.Add("");
        lines.Add("## Safety");
        lines.Add("");
        lines.Add("- All paths were checked to be inside the project root.");
        lines.Add("- Output PEX is a project-local copy generated by the controlled Mutagen adapter.");
        lines.Add("- No real Skyrim/Fallout 4, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.");
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static void WriteVerificationReport(
        string reportPath,
        string projectRoot,
        string inputPex,
        string translationJsonl,
        string outputPex,
        string game,
        GameCategory category,
        string capabilityLevel,
        int rowsParsed,
        int usableRows,
        int expectedReplacements,
        PexStructure? inputStructure,
        PexStructure? outputStructure,
        IReadOnlyCollection<string> errors,
        PexCallSiteClassifier classifier,
        PexCompatibilityMetadata? inputCompatibility,
        PexCompatibilityMetadata? outputCompatibility)
    {
        var lines = new List<string>
        {
            "# Mutagen PEX Independent Verification Report",
            "",
            $"- game_id: {game}",
            $"- pex_category: {category}",
            $"- capability_level: {capabilityLevel}",
            $"- semantic_registry: {(classifier.UsesSemanticRegistry ? Path.GetFileName(classifier.RegistryPath) : "not-used")}",
            $"- semantic_registry_sha256: {classifier.RegistrySha256}",
            $"- input_pex_layout: {inputCompatibility?.Layout ?? ""}",
            $"- output_pex_layout: {outputCompatibility?.Layout ?? ""}",
            $"- input_post_string_table_payload_sha256: {inputCompatibility?.PostStringTablePayloadSha256 ?? ""}",
            $"- output_post_string_table_payload_sha256: {outputCompatibility?.PostStringTablePayloadSha256 ?? ""}",
            $"- Verification mode: read-only",
            $"- Input PEX: {Relative(projectRoot, inputPex)}",
            $"- Translation JSONL: {Relative(projectRoot, translationJsonl)}",
            $"- Output PEX: {Relative(projectRoot, outputPex)}",
            $"- Input SHA256: {FileSha256(inputPex)}",
            $"- Translation JSONL SHA256: {FileSha256(translationJsonl)}",
            $"- Output SHA256: {FileSha256(outputPex)}",
            $"- Rows parsed: {rowsParsed}",
            $"- Usable rows: {usableRows}",
            $"- Expected replacements: {expectedReplacements}",
            $"- Input objects: {inputStructure?.Objects.ToString() ?? ""}",
            $"- Output objects: {outputStructure?.Objects.ToString() ?? ""}",
            $"- Input states: {inputStructure?.States.ToString() ?? ""}",
            $"- Output states: {outputStructure?.States.ToString() ?? ""}",
            $"- Input functions: {inputStructure?.Functions.ToString() ?? ""}",
            $"- Output functions: {outputStructure?.Functions.ToString() ?? ""}",
            $"- Input instructions: {inputStructure?.Instructions.ToString() ?? ""}",
            $"- Output instructions: {outputStructure?.Instructions.ToString() ?? ""}",
            $"- Verification passed: {errors.Count == 0}",
            "",
            "## Errors",
            "",
        };
        lines.AddRange(errors.Count == 0
            ? ["No verification errors."]
            : errors.Select(error => $"- {error}"));
        lines.AddRange(
        [
            "",
            "## Verification Scope",
            "",
            "- Loaded original and output with the same requested GameCategory.",
            "- Recomputed translation authorization from the original PEX and translation JSONL.",
            "- Compared target occurrences, all non-target instruction occurrences, non-instruction metadata strings, and structure counts.",
            "- Did not write, patch, replace, or delete any PEX binary.",
        ]);
        File.WriteAllLines(reportPath, lines, new UTF8Encoding(false));
    }

    private static string FileSha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream));
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

    private static string RiskForOccurrence(PexStringOccurrence occurrence)
    {
        return occurrence.Classification switch
        {
            "visible" => "candidate",
            "protected" => "protected-logic",
            "manual_review" => "manual-review",
            _ => ClassifyRisk(occurrence.Text),
        };
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
        EnsureNoReparsePointComponents(childFull, projectFull, label);
    }

    private static void EnsurePexOutputPath(string outputPex, string projectRoot)
    {
        var outputFull = FullPath(outputPex);
        var allowedRoots = new[]
        {
            FullPath(Path.Combine(projectRoot, "out")),
            FullPath(Path.Combine(projectRoot, "translated", "tool_outputs")),
        };
        if (!allowedRoots.Any(root =>
                outputFull.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidOperationException(
                $"output PEX must be under out/ or translated/tool_outputs/: {outputFull}");
        }
    }

    private static void EnsurePexExtension(string path, string label)
    {
        if (!string.Equals(Path.GetExtension(path), ".pex", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} must be a .pex file: {path}");
        }
    }

    private static void EnsureJsonlExtension(string path, string label)
    {
        if (!string.Equals(Path.GetExtension(path), ".jsonl", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} must be a .jsonl file: {path}");
        }
    }

    private static void EnsureMarkdownExtension(string path, string label)
    {
        if (!string.Equals(Path.GetExtension(path), ".md", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"{label} must be a .md file: {path}");
        }
    }

    private static void EnsureDistinctPaths(params (string Label, string Path)[] paths)
    {
        for (var left = 0; left < paths.Length; left++)
        {
            for (var right = left + 1; right < paths.Length; right++)
            {
                if (string.Equals(paths[left].Path, paths[right].Path, StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException(
                        $"{paths[left].Label} and {paths[right].Label} must use distinct paths.");
                }
                if (!File.Exists(paths[left].Path) || !File.Exists(paths[right].Path))
                {
                    continue;
                }
                FileIdentity leftIdentity;
                FileIdentity rightIdentity;
                try
                {
                    leftIdentity = GetFileIdentity(paths[left].Path);
                    rightIdentity = GetFileIdentity(paths[right].Path);
                }
                catch (Exception ex)
                {
                    throw new InvalidOperationException(
                        $"Unable to verify file identity for {paths[left].Label} and {paths[right].Label}; refusing existing path combination.",
                        ex);
                }
                if (leftIdentity == rightIdentity)
                {
                    throw new InvalidOperationException(
                        $"{paths[left].Label} and {paths[right].Label} resolve to the same existing file identity.");
                }
            }
        }
    }

    private static void EnsureNoReparsePointComponents(
        string childPath,
        string projectRoot,
        string label)
    {
        var relative = Path.GetRelativePath(projectRoot, childPath);
        if (relative == ".")
        {
            return;
        }
        var current = projectRoot;
        foreach (var segment in relative.Split(
                     [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
                     StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, segment);
            if (!File.Exists(current) && !Directory.Exists(current))
            {
                break;
            }
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidOperationException(
                    $"{label} traverses a symlink or reparse point: {current}");
            }
        }
    }

    private static FileIdentity GetFileIdentity(string path)
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Reliable hardlink identity checks are required for existing PEX adapter paths.");
        }
        using SafeFileHandle handle = File.OpenHandle(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete,
            FileOptions.None);
        if (!GetFileInformationByHandle(handle, out var information))
        {
            throw new IOException(
                $"Could not read file identity for {path}.",
                Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
        }
        return new FileIdentity(
            information.VolumeSerialNumber,
            ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out ByHandleFileInformation information);

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

    private readonly record struct FileIdentity(uint VolumeSerialNumber, ulong FileIndex);

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

    private sealed record TranslationRow(
        int LineNumber,
        int SchemaVersion,
        string GameId,
        string ModName,
        string ObjectName,
        string StateName,
        string FunctionName,
        string OpCode,
        string OpcodeForm,
        int InstructionIndex,
        int ArgumentIndex,
        string Callee,
        int SemanticArgumentIndex,
        string SemanticArgumentRole,
        string VisibilityBasis,
        string Classification,
        string ValueKind,
        bool IsDirectLiteral,
        string Source,
        string Target,
        bool IgnoreCase,
        string Risk);

    private sealed record PexStructure(int Objects, int States, int Functions, int Instructions);

    private sealed record Replacement(
        string ObjectName,
        string StateName,
        string FunctionName,
        string OpCode,
        int InstructionIndex,
        int ArgumentIndex,
        string Callee,
        int SemanticArgumentIndex,
        string SemanticArgumentRole,
        string Source,
        string Target);

    private sealed class ApplyResult
    {
        public HashSet<string> FoundSources { get; } = new(StringComparer.Ordinal);
        public HashSet<int> FoundRowLines { get; } = [];
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
        int SemanticArgumentIndex,
        PexObjectVariableData Argument,
        bool IsDirectLiteral,
        string Callee,
        string SemanticArgumentRole,
        string VisibilityBasis,
        string Classification)
    {
        public string Text => RepairUtf8Mojibake(Argument.StringValue ?? "");
    }

    private sealed class ExportRow
    {
        public int schema_version { get; init; } = 2;
        public string game_id { get; init; } = "";
        public string ModName { get; init; } = "";
        public string Type { get; init; } = "PEX";
        public string Source { get; init; } = "";
        public string Result { get; init; } = "";
        public string risk { get; init; } = "";
        public string object_name { get; init; } = "";
        public string state_name { get; init; } = "";
        public string function_name { get; init; } = "";
        public string opcode { get; init; } = "";
        public string opcode_form { get; init; } = "";
        public int instruction_index { get; init; }
        public int argument_index { get; init; }
        public string callee { get; init; } = "";
        public int semantic_argument_index { get; init; } = -1;
        public string semantic_argument_role { get; init; } = "";
        public string visibility_basis { get; init; } = "";
        public string classification { get; init; } = "";
        public string value_kind { get; init; } = "";
        public bool is_direct_literal { get; init; }
        public string notes { get; init; } = "";

        public static ExportRow FromOccurrence(PexStringOccurrence occurrence, string game)
        {
            var risk = occurrence.Classification switch
            {
                "visible" => "candidate",
                "protected" => "protected-logic",
                "manual_review" => "manual-review",
                _ => ClassifyRisk(occurrence.Text),
            };
            return new ExportRow
            {
                game_id = game,
                ModName = occurrence.FileName,
                Source = occurrence.Text,
                risk = risk,
                object_name = occurrence.ObjectName,
                state_name = occurrence.StateName,
                function_name = occurrence.FunctionName,
                opcode = occurrence.OpCode,
                opcode_form = occurrence.OpCode,
                instruction_index = occurrence.InstructionIndex,
                argument_index = occurrence.ArgumentIndex,
                callee = occurrence.Callee,
                semantic_argument_index = occurrence.SemanticArgumentIndex,
                semantic_argument_role = occurrence.SemanticArgumentRole,
                visibility_basis = occurrence.VisibilityBasis,
                classification = occurrence.Classification,
                value_kind = occurrence.Argument.VariableType.ToString(),
                is_direct_literal = occurrence.IsDirectLiteral,
                notes = risk switch
                {
                    "candidate" when occurrence.Classification == "visible" => "Direct player-visible literal authorized by the active Fallout 4 API registry.",
                    "candidate" => "Instruction string candidate; confirm player visibility before writeback.",
                    "manual-review" => "Semantic evidence does not authorize writeback; keep unchanged.",
                    _ => "Protected logic, protocol, diagnostic, or configuration value; keep unchanged.",
                },
            };
        }
    }

    private sealed class Options
    {
        public string Command { get; private set; } = "";
        public string? Game { get; private set; }
        public string? PexCategory { get; private set; }
        public string? CapabilityLevel { get; private set; }
        public string? ProjectRoot { get; private set; }
        public string? InputPex { get; private set; }
        public string? TranslationJsonl { get; private set; }
        public string? OutputPex { get; private set; }
        public string? OutputJsonl { get; private set; }
        public string? Report { get; private set; }
        public string? VisibleApiRegistry { get; private set; }
        public bool DryRun { get; private set; }
        public bool AllowExperimentalWriteback { get; private set; }

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
                    case "--game":
                        options.Game = Next(args, ref index, arg);
                        break;
                    case "--pex-category":
                        options.PexCategory = Next(args, ref index, arg);
                        break;
                    case "--capability-level":
                        options.CapabilityLevel = Next(args, ref index, arg);
                        break;
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
                    case "--visible-api-registry":
                        options.VisibleApiRegistry = Next(args, ref index, arg);
                        break;
                    case "--dry-run":
                        options.DryRun = true;
                        break;
                    case "--allow-experimental-writeback":
                        options.AllowExperimentalWriteback = true;
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
