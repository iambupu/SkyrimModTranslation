using Mutagen.Bethesda.Fallout4;
using Mutagen.Bethesda.Plugins;

internal sealed record Fallout4PluginFieldKey(
    string RecordType,
    FormKey FormKey,
    string SubrecordType);

internal sealed record Fallout4PluginExportField(
    string FieldPath,
    string EditorId,
    string Source);

internal interface IFallout4PluginFieldBinding
{
    string RecordType { get; }
    string SubrecordType { get; }
    string FieldPath { get; }

    void Collect(
        Fallout4Mod mod,
        IDictionary<Fallout4PluginFieldKey, Fallout4PluginExportField> fields);

    void Apply(Fallout4Mod mod, TranslationRow row, AdapterResult result);
}

internal static class Fallout4PluginFieldRegistry
{
    private static readonly IReadOnlyList<IFallout4PluginFieldBinding> Bindings =
    [
        Bind("WEAP", "FULL", "Name", static mod => mod.Weapons, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("ARMO", "FULL", "Name", static mod => mod.Armors, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("MISC", "FULL", "Name", static mod => mod.MiscItems, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("ALCH", "FULL", "Name", static mod => mod.Ingestibles, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("CELL", "FULL", "Name", static mod => EnumerateCells(mod), static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("WRLD", "FULL", "Name", static mod => mod.Worldspaces, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("PERK", "FULL", "Name", static mod => mod.Perks, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("PERK", "DESC", "Description", static mod => mod.Perks, static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "", static (item, value) => item.Description = value),
        Bind("MGEF", "FULL", "Name", static mod => mod.MagicEffects, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("MGEF", "DNAM", "Description", static mod => mod.MagicEffects, static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "", static (item, value) => item.Description = value),
        Bind("SPEL", "FULL", "Name", static mod => mod.Spells, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
        Bind("SPEL", "DESC", "Description", static mod => mod.Spells, static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "", static (item, value) => item.Description = value),
        Bind("MESG", "DESC", "Description", static mod => mod.Messages, static item => item.FormKey, static item => item.EditorID, static item => item.Description?.String ?? "", static (item, value) => item.Description = value),
        Bind("QUST", "FULL", "Name", static mod => mod.Quests, static item => item.FormKey, static item => item.EditorID, static item => item.Name?.String ?? "", static (item, value) => item.Name = value),
    ];

    public static IReadOnlyDictionary<(string Record, string Subrecord), string> ContractFields { get; } =
        Bindings.ToDictionary(
            static binding => (binding.RecordType, binding.SubrecordType),
            static binding => binding.FieldPath);

    public static Dictionary<Fallout4PluginFieldKey, Fallout4PluginExportField> BuildExportFields(
        Fallout4Mod mod)
    {
        var fields = new Dictionary<Fallout4PluginFieldKey, Fallout4PluginExportField>();
        foreach (var binding in Bindings)
        {
            binding.Collect(mod, fields);
        }
        return fields;
    }

    public static bool TryApply(
        Fallout4Mod mod,
        TranslationRow row,
        AdapterResult result)
    {
        var binding = Bindings.FirstOrDefault(candidate =>
            candidate.RecordType == row.RecordType
            && candidate.SubrecordType == row.SubrecordType
            && candidate.FieldPath == row.FieldPath);
        if (binding is null)
        {
            return false;
        }
        binding.Apply(mod, row, result);
        return true;
    }

    private static IFallout4PluginFieldBinding Bind<TRecord>(
        string recordType,
        string subrecordType,
        string fieldPath,
        Func<Fallout4Mod, IEnumerable<TRecord>> records,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source,
        Action<TRecord, string> assign)
        where TRecord : class =>
        new Fallout4PluginFieldBinding<TRecord>(
            recordType,
            subrecordType,
            fieldPath,
            records,
            formKey,
            editorId,
            source,
            assign);

    private static IEnumerable<Cell> EnumerateCells(Fallout4Mod mod)
    {
        foreach (var block in mod.Cells.Records)
        foreach (var subBlock in block.SubBlocks)
        foreach (var cell in subBlock.Cells)
            yield return cell;
    }

    private sealed class Fallout4PluginFieldBinding<TRecord>(
        string recordType,
        string subrecordType,
        string fieldPath,
        Func<Fallout4Mod, IEnumerable<TRecord>> records,
        Func<TRecord, FormKey> formKey,
        Func<TRecord, string?> editorId,
        Func<TRecord, string> source,
        Action<TRecord, string> assign) : IFallout4PluginFieldBinding
        where TRecord : class
    {
        public string RecordType { get; } = recordType;
        public string SubrecordType { get; } = subrecordType;
        public string FieldPath { get; } = fieldPath;

        public void Collect(
            Fallout4Mod mod,
            IDictionary<Fallout4PluginFieldKey, Fallout4PluginExportField> fields)
        {
            foreach (var record in records(mod))
            {
                var value = source(record);
                if (string.IsNullOrWhiteSpace(value))
                {
                    continue;
                }
                var key = new Fallout4PluginFieldKey(RecordType, formKey(record), SubrecordType);
                if (!fields.TryAdd(
                        key,
                        new Fallout4PluginExportField(FieldPath, editorId(record) ?? string.Empty, value)))
                {
                    throw new InvalidDataException($"duplicate Mutagen export identity: {key}");
                }
            }
        }

        public void Apply(Fallout4Mod mod, TranslationRow row, AdapterResult result)
        {
            var record = records(mod).FirstOrDefault(item =>
                row.ResolvedFormKey is FormKey expected
                && expected == formKey(item)
                && (string.IsNullOrWhiteSpace(row.EditorId)
                    || string.Equals(
                        row.EditorId,
                        editorId(item) ?? string.Empty,
                        StringComparison.OrdinalIgnoreCase)));
            if (record is null)
            {
                result.Missing.Add(Describe(row, "record identity not found"));
                return;
            }
            if (!string.Equals(source(record), row.Source, StringComparison.Ordinal))
            {
                result.Missing.Add(Describe(row, "source text does not match current record value"));
                return;
            }
            assign(record, row.Target);
            result.Applied.Add(Describe(row, row.FieldPath));
        }

        private static string Describe(TranslationRow row, string action) =>
            $"{row.RecordType} {row.FormId} {row.FieldPath} {row.EditorId}: {action}";
    }
}
