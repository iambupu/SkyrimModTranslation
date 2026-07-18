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

        var dataSize = checked((int)BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(4, 4)));
        if (dataSize > MaxHeaderDataBytes)
        {
            throw new InvalidDataException(
                $"TES4 header data exceeds the bounded limit of {MaxHeaderDataBytes} bytes");
        }
        if (checked(HeaderLength + (long)dataSize) > stream.Length)
        {
            throw new InvalidDataException("TES4 header data exceeds the plugin file boundary");
        }
        var data = new byte[dataSize];
        stream.ReadExactly(data);

        var masters = new List<ModKey>();
        var offset = 0;
        while (offset < data.Length)
        {
            if (offset + 6 > data.Length)
            {
                throw new InvalidDataException("truncated TES4 subrecord header");
            }
            var signature = Encoding.ASCII.GetString(data, offset, 4);
            var payloadSize = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 4, 2));
            offset += 6;
            if (offset + payloadSize > data.Length)
            {
                throw new InvalidDataException($"TES4 {signature} payload exceeds the header boundary");
            }
            if (string.Equals(signature, "MAST", StringComparison.Ordinal))
            {
                var payload = data.AsSpan(offset, payloadSize);
                var nul = payload.IndexOf((byte)0);
                var nameBytes = nul >= 0 ? payload[..nul] : payload;
                var name = Encoding.UTF8.GetString(nameBytes).Trim();
                if (string.IsNullOrWhiteSpace(name))
                {
                    throw new InvalidDataException("TES4 MAST contains an empty master name");
                }
                masters.Add(ModKey.FromNameAndExtension(name));
            }
            offset += payloadSize;
        }

        return new(
            ModKey.FromNameAndExtension(Path.GetFileName(pluginPath)),
            BinaryPrimitives.ReadUInt32LittleEndian(header.Slice(8, 4)),
            masters);
    }
}
