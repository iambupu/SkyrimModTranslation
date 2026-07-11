internal sealed class AdapterResult
{
    public List<string> Applied { get; } = [];
    public List<string> Missing { get; } = [];
    public List<string> Unsupported { get; } = [];
    public List<string> Skipped { get; } = [];
}
