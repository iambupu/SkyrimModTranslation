internal static class AtomicPluginOutput
{
    public static void PrepareTarget(string outputPlugin)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(outputPlugin)!);
        DeleteIfExists(outputPlugin);
    }

    public static string CreateTemporaryPath(string outputPlugin)
    {
        var directory = Path.GetDirectoryName(outputPlugin)!;
        var extension = Path.GetExtension(outputPlugin);
        var stem = Path.GetFileNameWithoutExtension(outputPlugin);
        return Path.Combine(directory, $".{stem}.{Guid.NewGuid():N}.tmp{extension}");
    }

    public static void Commit(string temporaryPlugin, string outputPlugin)
    {
        if (File.Exists(outputPlugin))
        {
            throw new IOException($"Output target unexpectedly exists before atomic commit: {outputPlugin}");
        }
        File.Move(temporaryPlugin, outputPlugin);
    }

    public static void CleanupFailure(string temporaryPlugin, string outputPlugin)
    {
        DeleteIfExists(temporaryPlugin);
        DeleteIfExists(outputPlugin);
    }

    private static void DeleteIfExists(string path)
    {
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }
}
