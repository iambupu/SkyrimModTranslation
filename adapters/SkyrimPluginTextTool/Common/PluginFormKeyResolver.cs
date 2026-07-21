using System.Globalization;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Masters;
using Mutagen.Bethesda.Plugins.Records;

internal sealed record PluginCanonicalFormIdentity(
    FormKey FormKey,
    uint LocalId,
    string MasterStyle,
    string EvidenceSource,
    bool RequiresCanonicalRow,
    bool? OwnerIsLight);

internal sealed class PluginFormKeyResolver
{
    private readonly ModKey _currentMod;
    private readonly ModKey[] _masters;
    private readonly PluginMasterStyleContext? _masterStyleContext;
    private readonly IReadOnlySeparatedMasterPackage? _masterPackage;

    public PluginFormKeyResolver(
        IModGetter mod,
        PluginMasterStyleContext? masterStyleContext = null)
    {
        _currentMod = mod.ModKey;
        _masters = mod.MasterReferences.Select(static reference => reference.Master).ToArray();
        _masterStyleContext = masterStyleContext;
        _masterPackage = masterStyleContext?.MasterPackage;
    }

    public bool TryResolve(string rawFormId, out FormKey formKey, out string reason)
    {
        return TryResolve(rawFormId, out formKey, out _, out reason);
    }

    public bool TryResolve(
        string rawFormId,
        out FormKey formKey,
        out PluginCanonicalFormIdentity identity,
        out string reason)
    {
        formKey = default;
        identity = new(default, 0, string.Empty, string.Empty, false, null);
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

        if (_masterPackage is not null)
        {
            var contextualMasterIndex = (int)(raw >> 24);
            if (contextualMasterIndex > _masters.Length)
            {
                reason = raw >> 24 == 0xFE
                    ? "raw 0xFE/load-order FormID cannot authorize plugin writeback; use the plugin-local FormID with canonical owner evidence"
                    : $"form_id master index {contextualMasterIndex} exceeds header master count {_masters.Length}";
                return false;
            }
            try
            {
                formKey = _masterPackage.GetFormKey(new FormID(raw), reference: true);
            }
            catch (Exception exc)
            {
                reason = $"Mutagen light FormKey resolution failed for {raw:X8}: {exc.Message}";
                return false;
            }
            if (_masterStyleContext is null
                || !_masterStyleContext.TryGetStyle(formKey, out var resolvedStyle))
            {
                identity = new(
                    formKey,
                    formKey.ID,
                    "unknown",
                    "unresolved:unseparated-master-order",
                    true,
                    null);
                reason = string.Empty;
                return true;
            }
            if (resolvedStyle.Style == MasterStyle.Small && formKey.ID > 0xFFF)
            {
                reason = $"light local_id must fit in 12 bits: {formKey.ID:X}";
                return false;
            }
            identity = new(
                formKey,
                formKey.ID,
                PluginMasterStyleContext.StyleName(resolvedStyle.Style),
                resolvedStyle.EvidenceSource,
                resolvedStyle.Style == MasterStyle.Small,
                resolvedStyle.Style == MasterStyle.Small);
            reason = string.Empty;
            return true;
        }

        var masterIndex = (int)(raw >> 24);
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
        var ownerIsCurrent = owner == _currentMod;
        ResolvedMasterStyle? ordinaryStyle = null;
        var ownerHasKnownStyle = _masterStyleContext is not null
            && _masterStyleContext.TryGetStyle(owner, out ordinaryStyle);
        var gameId = _masterStyleContext?.GameRelease switch
        {
            Mutagen.Bethesda.GameRelease.SkyrimSE => "skyrim-se",
            Mutagen.Bethesda.GameRelease.Fallout4 => "fallout4",
            _ => string.Empty,
        };
        var ownerIsKnownFull = !ownerIsCurrent
            && !string.IsNullOrEmpty(gameId)
            && GameMasterStylePolicy.IsKnownFullMaster(gameId, owner);
        identity = ownerIsCurrent
            ? new(
                formKey,
                formKey.ID,
                "full",
                "ordinary-schema-v2",
                false,
                false)
            : ownerHasKnownStyle
            ? new(
                formKey,
                formKey.ID,
                PluginMasterStyleContext.StyleName(ordinaryStyle!.Style),
                ordinaryStyle.EvidenceSource,
                ordinaryStyle.Style == MasterStyle.Small,
                ordinaryStyle.Style == MasterStyle.Small)
            : ownerIsKnownFull
            ? new(
                formKey,
                formKey.ID,
                "full",
                "game-profile:known-full",
                false,
                false)
            : new(
                formKey,
                formKey.ID,
                "unknown",
                "unresolved:unseparated-master-order",
                true,
                null);
        reason = string.Empty;
        return true;
    }

    public bool TryBindRow(
        TranslationRow row,
        out FormKey formKey,
        out string reason)
    {
        if (!TryResolve(row.FormId, out formKey, out var identity, out reason))
        {
            return false;
        }

        if (identity.OwnerIsLight is null)
        {
            reason = $"master_style_unknown: translation target owner {identity.FormKey.ModKey} requires target-scoped master-style evidence";
            return false;
        }

        var hasCanonicalField = !string.IsNullOrWhiteSpace(row.OwnerModKey)
            || row.LocalId is not null
            || !string.IsNullOrWhiteSpace(row.MasterStyle)
            || !string.IsNullOrWhiteSpace(row.MasterStyleEvidence);
        if (!identity.RequiresCanonicalRow && !hasCanonicalField)
        {
            return true;
        }
        if (string.IsNullOrWhiteSpace(row.OwnerModKey)
            || row.LocalId is null
            || string.IsNullOrWhiteSpace(row.MasterStyle)
            || string.IsNullOrWhiteSpace(row.MasterStyleEvidence))
        {
            reason = "light-aware row requires owner_mod_key, local_id, master_style, and master_style_evidence";
            return false;
        }
        if (!ModKey.TryFromNameAndExtension(row.OwnerModKey, out var owner))
        {
            reason = $"invalid owner_mod_key: {row.OwnerModKey}";
            return false;
        }
        if (string.Equals(row.MasterStyle, "light", StringComparison.OrdinalIgnoreCase)
            && row.LocalId > 0xFFF)
        {
            reason = $"light local_id must fit in 12 bits: {row.LocalId:X}";
            return false;
        }
        if (owner != identity.FormKey.ModKey
            || row.LocalId.Value != identity.LocalId
            || !string.Equals(row.MasterStyle, identity.MasterStyle, StringComparison.OrdinalIgnoreCase))
        {
            reason = "canonical FormKey identity does not match resolved owner, local_id, or master_style";
            return false;
        }
        if (!string.Equals(
                row.MasterStyleEvidence,
                identity.EvidenceSource,
                StringComparison.Ordinal))
        {
            reason = "master_style_evidence does not match the active project-local context";
            return false;
        }
        return true;
    }
}
