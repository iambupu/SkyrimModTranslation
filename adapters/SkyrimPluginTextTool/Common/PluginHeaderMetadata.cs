using System.Buffers.Binary;
using System.Text;
using Mutagen.Bethesda.Plugins;

internal sealed record PluginHeaderMetadata(
    ModKey ModKey,
    uint Flags,
    IReadOnlyList<ModKey> Masters)
{
    private const uint SmallFlag = 0x00000200;
    private const int HeaderLength = 24;
    private const int MaxHeaderDataBytes = 16 * 1024 * 1024;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    public bool LightByExtension =>
        string.Equals(ModKey.Type.ToString(), "Light", StringComparison.OrdinalIgnoreCase);

    public bool SmallFlagged => (Flags & SmallFlag) != 0;

    public bool IsSmall => LightByExtension || SmallFlagged;

    public static PluginHeaderMetadata Read(string pluginPath)
    {
        using var stream = new FileStream(
            pluginPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read);
        if (stream.Length < HeaderLength)
        {
            throw new InvalidDataException("plugin does not contain a complete TES4 header");
        }

        Span<byte> header = stackalloc byte[HeaderLength];
        stream.ReadExactly(header);
        if (!header[..4].SequenceEqual("TES4"u8))
        {
            throw new InvalidDataException("plugin does not contain a complete TES4 header");
        }

        var rawDataSize = BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(4, 4));
        if (rawDataSize > MaxHeaderDataBytes)
        {
            throw new InvalidDataException(
                $"TES4 header data exceeds the bounded limit of {MaxHeaderDataBytes} bytes");
        }
        var dataSize = (int)rawDataSize;
        if (checked(HeaderLength + (long)dataSize) > stream.Length)
        {
            throw new InvalidDataException("TES4 header data exceeds the plugin file boundary");
        }
        var data = new byte[dataSize];
        stream.ReadExactly(data);

        var masters = new List<ModKey>();
        var knownMasters = new HashSet<ModKey>();
        foreach (var subrecord in Tes4SubrecordReader.Read(data, HeaderLength, "TES4"))
        {
            if (string.Equals(subrecord.Signature, "MAST", StringComparison.Ordinal))
            {
                var relativePayloadOffset = checked(subrecord.PayloadOffset - HeaderLength);
                var payload = data.AsSpan(
                    relativePayloadOffset,
                    checked((int)subrecord.PayloadSize));
                var nul = payload.IndexOf((byte)0);
                var nameBytes = nul >= 0 ? payload[..nul] : payload;
                string name;
                try
                {
                    name = StrictUtf8.GetString(nameBytes).Trim();
                }
                catch (DecoderFallbackException exception)
                {
                    throw new InvalidDataException("TES4 MAST contains invalid UTF-8", exception);
                }
                if (string.IsNullOrWhiteSpace(name))
                {
                    throw new InvalidDataException("TES4 MAST contains an empty master name");
                }
                if (name.Contains('/')
                    || name.Contains('\\')
                    || !string.Equals(Path.GetFileName(name), name, StringComparison.Ordinal)
                    || Path.GetExtension(name).ToLowerInvariant() is not (".esp" or ".esm" or ".esl"))
                {
                    throw new InvalidDataException($"TES4 MAST contains an invalid master name: {name}");
                }
                var master = ModKey.FromNameAndExtension(name);
                if (!knownMasters.Add(master))
                {
                    throw new InvalidDataException($"TES4 contains duplicate MAST {master}");
                }
                masters.Add(master);
            }
        }

        return new(
            ModKey.FromNameAndExtension(Path.GetFileName(pluginPath)),
            BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(8, 4)),
            masters);
    }
}
