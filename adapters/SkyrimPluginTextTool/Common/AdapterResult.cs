internal sealed class AdapterResult
{
    public PluginTraits Traits { get; set; } = PluginTraits.Unknown;
    public List<string> Applied { get; } = [];
    public List<string> Missing { get; } = [];
    public List<string> Unsupported { get; } = [];
    public List<string> Skipped { get; } = [];
    public string ReparseTarget { get; set; } = "temporary-output";
    public bool ReparseSucceeded { get; set; }
    public uint InputRecordCount { get; set; }
    public uint OutputRecordCount { get; set; }
    public bool RecordCountPreserved { get; set; }
    public string[] InputFormKeys { get; set; } = [];
    public string[] OutputFormKeys { get; set; } = [];
    public bool FormKeySetPreserved { get; set; }
    public string[] InputMasters { get; set; } = [];
    public string[] OutputMasters { get; set; } = [];
    public bool MastersPreserved { get; set; }
    public bool BinaryInvariantVerified { get; set; }
    public int BinaryInvariantRecordsChecked { get; set; }
    public int BinaryInvariantTargetsVerified { get; set; }
    public string[] AllowedHeaderChanges { get; set; } = [];
    public string[] BinaryInvariantIssues { get; set; } = [];
    public bool StructuralValidationSucceeded =>
        ReparseSucceeded && RecordCountPreserved && FormKeySetPreserved && MastersPreserved && BinaryInvariantVerified;

    public void ApplyBinaryInvariant(PluginBinaryInvariantResult invariant)
    {
        BinaryInvariantVerified = invariant.Verified;
        BinaryInvariantRecordsChecked = invariant.RecordsChecked;
        BinaryInvariantTargetsVerified = invariant.TargetsVerified;
        AllowedHeaderChanges = invariant.AllowedHeaderChanges;
        BinaryInvariantIssues = invariant.Issues;
        if (!invariant.Verified)
        {
            Unsupported.AddRange(invariant.Issues.Select(issue => $"Parsed structural/payload invariant: {issue}"));
        }
    }

}
