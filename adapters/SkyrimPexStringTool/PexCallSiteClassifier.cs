using Mutagen.Bethesda.Pex;

internal sealed record PexClassifiedArgument(
    int RawArgumentIndex,
    int SemanticArgumentIndex,
    PexObjectVariableData Argument,
    bool IsDirectLiteral,
    string Callee,
    string SemanticArgumentRole,
    string VisibilityBasis,
    string Classification);

internal sealed class PexCallSiteClassifier
{
    private readonly PexVisibleApiRegistry? _registry;

    private PexCallSiteClassifier(PexVisibleApiRegistry? registry)
    {
        _registry = registry;
    }

    public bool UsesSemanticRegistry => _registry is not null;
    public string RegistryPath => _registry?.SourcePath ?? "";
    public string RegistrySha256 => _registry?.SourceSha256 ?? "";

    public static PexCallSiteClassifier ForGame(
        string gameId,
        string? visibleApiRegistryPath = null)
    {
        return new PexCallSiteClassifier(
            PexVisibleApiRegistry.LoadForGame(gameId, visibleApiRegistryPath));
    }

    public IEnumerable<PexClassifiedArgument> ClassifyInstruction(
        PexObjectFunctionInstruction instruction,
        string parentClassName)
    {
        if (_registry is null)
        {
            for (var index = 0; index < instruction.Arguments.Count; index++)
            {
                var argument = instruction.Arguments[index];
                if (argument.VariableType == VariableType.String && !string.IsNullOrEmpty(argument.StringValue))
                {
                    yield return new PexClassifiedArgument(index, -1, argument, true, "", "", "", "");
                }
            }
            yield break;
        }

        var call = ParseCall(instruction, parentClassName);
        if (call is null)
        {
            foreach (var item in ManualReviewStringArguments(instruction, "non_call_instruction"))
            {
                yield return item;
            }
            yield break;
        }
        if (!call.ArgumentLayoutValid)
        {
            foreach (var item in ManualReviewStringArguments(instruction, "invalid_call_argument_layout"))
            {
                yield return item;
            }
            yield break;
        }

        var emittedRawIndexes = new HashSet<int>();
        for (var semanticIndex = 0; semanticIndex < call.ArgumentCount; semanticIndex++)
        {
            var rawIndex = call.FirstArgumentIndex + semanticIndex;
            var argument = instruction.Arguments[rawIndex];
            var directLiteral = argument.VariableType == VariableType.String;
            PexVisibleArgumentRule? rule = null;
            var matched = call.CalleeResolved
                && _registry.TryGetArgument(call.Callee, call.Opcode, semanticIndex, out rule);
            if (directLiteral && !string.IsNullOrEmpty(argument.StringValue))
            {
                emittedRawIndexes.Add(rawIndex);
                yield return new PexClassifiedArgument(
                    rawIndex,
                    semanticIndex,
                    argument,
                    true,
                    call.Callee,
                    matched ? rule!.SemanticRole : $"unresolved_argument_{semanticIndex}",
                    matched ? rule!.VisibilityBasis : "unmatched_api_registry",
                    matched ? rule!.Classification : "manual_review");
            }
            else if (matched)
            {
                emittedRawIndexes.Add(rawIndex);
                yield return new PexClassifiedArgument(
                    rawIndex,
                    semanticIndex,
                    argument,
                    false,
                    call.Callee,
                    rule!.SemanticRole,
                    $"dynamic_argument:{rule.VisibilityBasis}",
                    "manual_review");
            }
        }

        for (var rawIndex = 0; rawIndex < instruction.Arguments.Count; rawIndex++)
        {
            var argument = instruction.Arguments[rawIndex];
            if (emittedRawIndexes.Contains(rawIndex)
                || argument.VariableType != VariableType.String
                || string.IsNullOrEmpty(argument.StringValue))
            {
                continue;
            }
            yield return new PexClassifiedArgument(
                rawIndex,
                -1,
                argument,
                true,
                call.Callee,
                "call_metadata",
                "call_metadata_not_translatable",
                "manual_review");
        }
    }

    private static IEnumerable<PexClassifiedArgument> ManualReviewStringArguments(
        PexObjectFunctionInstruction instruction,
        string basis)
    {
        for (var index = 0; index < instruction.Arguments.Count; index++)
        {
            var argument = instruction.Arguments[index];
            if (argument.VariableType == VariableType.String && !string.IsNullOrEmpty(argument.StringValue))
            {
                yield return new PexClassifiedArgument(
                    index,
                    -1,
                    argument,
                    true,
                    "",
                    "unresolved_argument",
                    basis,
                    "manual_review");
            }
        }
    }

    private static ParsedCall? ParseCall(
        PexObjectFunctionInstruction instruction,
        string parentClassName)
    {
        var opcode = instruction.OpCode.ToString().ToUpperInvariant();
        int firstArgument;
        int countIndex;
        string owner;
        string method;
        switch (instruction.OpCode)
        {
            case InstructionOpcode.CALLSTATIC:
                if (instruction.Arguments.Count < 4)
                {
                    return new ParsedCall(opcode, "", false, 4, 0, false);
                }
                owner = ArgumentName(instruction.Arguments[0]);
                method = ArgumentName(instruction.Arguments[1]);
                firstArgument = 4;
                countIndex = 3;
                break;
            case InstructionOpcode.CALLPARENT:
                if (instruction.Arguments.Count < 3)
                {
                    return new ParsedCall(opcode, "", false, 3, 0, false);
                }
                owner = parentClassName;
                method = ArgumentName(instruction.Arguments[0]);
                firstArgument = 3;
                countIndex = 2;
                break;
            case InstructionOpcode.CALLMETHOD:
                if (instruction.Arguments.Count < 4)
                {
                    return new ParsedCall(opcode, "", false, 4, 0, false);
                }
                owner = "";
                method = ArgumentName(instruction.Arguments[0]);
                firstArgument = 4;
                countIndex = 3;
                break;
            default:
                return null;
        }

        var countArgument = instruction.Arguments[countIndex];
        var count = countArgument.VariableType == VariableType.Integer
            ? countArgument.IntValue ?? -1
            : -1;
        var layoutValid = count >= 0 && firstArgument + count == instruction.Arguments.Count;
        var calleeResolved = !string.IsNullOrWhiteSpace(owner) && !string.IsNullOrWhiteSpace(method);
        return new ParsedCall(
            opcode,
            calleeResolved ? $"{owner}.{method}" : method,
            calleeResolved,
            firstArgument,
            Math.Max(count, 0),
            layoutValid);
    }

    private static string ArgumentName(PexObjectVariableData argument)
    {
        return argument.StringValue?.Trim() ?? "";
    }

    private sealed record ParsedCall(
        string Opcode,
        string Callee,
        bool CalleeResolved,
        int FirstArgumentIndex,
        int ArgumentCount,
        bool ArgumentLayoutValid);
}
