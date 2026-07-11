using System.Text.Json.Serialization;
using Mutagen.Bethesda.Plugins;

internal sealed class TranslationRow
{
    [JsonPropertyName("schema_version")]
    public int SchemaVersion { get; set; } = 1;

    [JsonPropertyName("game_id")]
    public string GameId { get; set; } = "";

    [JsonPropertyName("plugin")]
    public string Plugin { get; set; } = "";

    [JsonPropertyName("record_type")]
    public string RecordType { get; set; } = "";

    [JsonPropertyName("form_id")]
    public string FormId { get; set; } = "";

    [JsonPropertyName("editor_id")]
    public string EditorId { get; set; } = "";

    [JsonPropertyName("field_path")]
    public string FieldPath { get; set; } = "";

    [JsonPropertyName("subrecord_type")]
    public string SubrecordType { get; set; } = "";

    [JsonPropertyName("subrecord_index")]
    public int SubrecordIndex { get; set; }

    [JsonPropertyName("source")]
    public string Source { get; set; } = "";

    [JsonPropertyName("target")]
    public string Target { get; set; } = "";

    [JsonPropertyName("risk")]
    public string Risk { get; set; } = "";

    [JsonPropertyName("writeback")]
    public string Writeback { get; set; } = "";

    [JsonIgnore]
    public FormKey? ResolvedFormKey { get; set; }
}
