internal sealed class AdapterResult
{
    public List<string> Applied { get; } = [];
    public List<string> Missing { get; } = [];
    public List<string> Unsupported { get; } = [];
    public List<string> Skipped { get; } = [];
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
    public bool StructuralValidationSucceeded =>
        ReparseSucceeded && RecordCountPreserved && FormKeySetPreserved && MastersPreserved;
}
