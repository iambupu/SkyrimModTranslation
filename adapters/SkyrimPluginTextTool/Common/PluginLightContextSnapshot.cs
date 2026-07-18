using Mutagen.Bethesda.Plugins;

internal sealed record PluginLightContextSnapshot(
    string CurrentStyle,
    bool SmallFlag,
    string[] MasterStyles)
{
    public static PluginLightContextSnapshot From(
        string pluginPath,
        PluginMasterStyleContext context)
    {
        var header = PluginHeaderMetadata.Read(pluginPath);
        var masterStyles = header.Masters.Select(master =>
        {
            if (!context.Required) return $"{master.FileName.String}|full";
            if (!context.TryGetStyle(master, out var resolved))
            {
                throw new InvalidDataException(
                    $"master-style verification has no evidence for {master}");
            }
            return $"{master.FileName.String}|{PluginMasterStyleContext.StyleName(resolved.Style)}";
        }).ToArray();
        return new(
            header.IsSmall ? "light" : "full",
            header.SmallFlagged,
            masterStyles);
    }

    public void ApplyComparison(
        PluginLightContextSnapshot output,
        AdapterResult result)
    {
        result.InputCurrentMasterStyle = CurrentStyle;
        result.OutputCurrentMasterStyle = output.CurrentStyle;
        result.CurrentMasterStylePreserved = string.Equals(
            CurrentStyle,
            output.CurrentStyle,
            StringComparison.Ordinal);
        result.InputMasterStyles = MasterStyles;
        result.OutputMasterStyles = output.MasterStyles;
        result.MasterStylesPreserved = MasterStyles.SequenceEqual(
            output.MasterStyles,
            StringComparer.OrdinalIgnoreCase);
        result.InputSmallFlag = SmallFlag;
        result.OutputSmallFlag = output.SmallFlag;
        result.SmallFlagPreserved = SmallFlag == output.SmallFlag;
    }
}
