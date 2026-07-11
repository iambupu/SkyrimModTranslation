using Mutagen.Bethesda.Plugins.Records;

internal sealed record PluginStructureSnapshot(
    uint RecordCount,
    string[] FormKeys,
    string[] Masters)
{
    public static PluginStructureSnapshot From(IModGetter mod)
    {
        return new PluginStructureSnapshot(
            mod.GetRecordCount(),
            mod.EnumerateMajorRecords()
                .Select(record => CanonicalFormKey(record.FormKey, mod.ModKey))
                .OrderBy(static value => value, StringComparer.OrdinalIgnoreCase)
                .ToArray(),
            mod.MasterReferences
                .Select(static reference => reference.Master.ToString())
                .ToArray());
    }

    private static string CanonicalFormKey(Mutagen.Bethesda.Plugins.FormKey formKey, Mutagen.Bethesda.Plugins.ModKey currentMod)
    {
        var owner = formKey.ModKey == currentMod ? "$SELF" : formKey.ModKey.ToString();
        return $"{owner}|{formKey.ID:X6}";
    }

    public void ApplyComparison(PluginStructureSnapshot output, AdapterResult result)
    {
        result.InputRecordCount = RecordCount;
        result.OutputRecordCount = output.RecordCount;
        result.RecordCountPreserved = RecordCount == output.RecordCount;
        result.InputFormKeys = FormKeys;
        result.OutputFormKeys = output.FormKeys;
        result.FormKeySetPreserved = FormKeys.SequenceEqual(output.FormKeys, StringComparer.OrdinalIgnoreCase);
        result.InputMasters = Masters;
        result.OutputMasters = output.Masters;
        result.MastersPreserved = Masters.SequenceEqual(output.Masters, StringComparer.OrdinalIgnoreCase);
    }
}
