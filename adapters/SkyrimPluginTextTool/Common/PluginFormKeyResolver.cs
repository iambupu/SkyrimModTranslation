using System.Globalization;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Records;

internal sealed class PluginFormKeyResolver
{
    private readonly ModKey _currentMod;
    private readonly ModKey[] _masters;

    public PluginFormKeyResolver(IModGetter mod)
    {
        _currentMod = mod.ModKey;
        _masters = mod.MasterReferences.Select(static reference => reference.Master).ToArray();
    }

    public bool TryResolve(string rawFormId, out FormKey formKey, out string reason)
    {
        formKey = default;
        var value = (rawFormId ?? string.Empty).Trim();
        if (value.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
        {
            value = value[2..];
        }
        if (value.Length != 8 || !uint.TryParse(value, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var raw))
        {
            reason = $"schema v2 form_id must be exactly 8 hexadecimal digits: {rawFormId}";
            return false;
        }

        var masterIndex = (int)(raw >> 24);
        if (masterIndex == 0xFE)
        {
            reason = "0xFE/light FormID is unsupported until a fixture-backed light plugin resolver is available";
            return false;
        }
        ModKey owner;
        if (masterIndex < _masters.Length)
        {
            owner = _masters[masterIndex];
        }
        else if (masterIndex == _masters.Length)
        {
            owner = _currentMod;
        }
        else
        {
            reason = $"form_id master index {masterIndex} exceeds header master count {_masters.Length}";
            return false;
        }

        formKey = new FormKey(owner, raw & 0x00FFFFFF);
        reason = string.Empty;
        return true;
    }
}
