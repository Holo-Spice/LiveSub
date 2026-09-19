using System.Formats.Tar;
using System.IO;
using System.IO.Compression;

namespace LiveSub.Launcher;

internal sealed record SetupProgress(string Component, string Stage, long Downloaded = 0, long DownloadTotal = 0, double BytesPerSecond = 0);

internal sealed class Installer
{
    private readonly DownloadService _downloads = new();

    public async Task InstallComponentAsync(string root, InstallComponent component, InstallState state, IProgress<SetupProgress> progress, CancellationToken cancellationToken)
    {
        if (state.IsComplete(root, component)) return;
        await state.ClaimAsync(root, component.Id, component.Target, cancellationToken);
        var target = InstallManifest.UnderRoot(root, component.Target);
        RejectLinks(root, target);
        if (File.Exists(target) || Directory.Exists(target))
        {
            bool valid = HasExpectedFiles(root, component) && Measure(target) == component.InstalledSizeBytes;
            if (valid && component.Sources[0].Archive == "none")
            {
                await using var existing = File.OpenRead(target);
                valid = Convert.ToHexString(await System.Security.Cryptography.SHA256.HashDataAsync(existing, cancellationToken))
                    .Equals(component.Sources[0].Sha256, StringComparison.OrdinalIgnoreCase);
            }
            if (valid)
            {
                await state.CompleteAsync(root, component, cancellationToken);
                return;
            }
            throw new IOException($"未完成目标 {component.Target} 与清单不符，不能覆盖；请检查该目录后重试或选择新目录。");
        }

        var temporary = InstallManifest.UnderRoot(root, $".setup/{component.Id}");
        Directory.CreateDirectory(temporary);
        long downloadedBefore = 0;
        long totalDownload = component.Sources.Sum(source => source.DownloadSizeBytes);
        var parts = new List<string>();
        for (int index = 0; index < component.Sources.Count; index++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var source = component.Sources[index];
            var part = Path.Combine(temporary, $"source-{index}.part");
            var offset = downloadedBefore;
            progress.Report(new SetupProgress(component.Name, "正在下载", offset, totalDownload));
            await _downloads.DownloadAsync(source, part,
                new Progress<DownloadProgress>(p => progress.Report(new SetupProgress(component.Name, "正在下载", offset + p.Bytes, totalDownload, p.BytesPerSecond))),
                cancellationToken);
            parts.Add(part);
            downloadedBefore += source.DownloadSizeBytes;
        }

        progress.Report(new SetupProgress(component.Name, "正在解压和校验"));
        var staged = Path.Combine(temporary, "staged");
        RejectLinks(root, staged);
        if (Directory.Exists(staged)) Directory.Delete(staged, recursive: true);
        Directory.CreateDirectory(staged);
        await Task.Run(() =>
        {
            for (int index = 0; index < parts.Count; index++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var source = component.Sources[index];
                switch (source.Archive)
                {
                    case "none": File.Copy(parts[index], Path.Combine(staged, "payload")); break;
                    case "zip": ExtractZip(parts[index], staged, source.StripComponents, cancellationToken); break;
                    case "tar.gz": ExtractTar(parts[index], staged, source.StripComponents, cancellationToken); break;
                    default: throw new InvalidDataException("未知归档类型。");
                }
            }
        }, cancellationToken);

        var payload = component.Sources[0].Archive == "none" ? Path.Combine(staged, "payload") : staged;
        if (Measure(payload) != component.InstalledSizeBytes)
            throw new InvalidDataException($"组件 {component.Name} 解压后大小与清单不符。");
        foreach (var relative in component.RequiredPaths)
        {
            string stagedRequired;
            if (component.Sources[0].Archive == "none") stagedRequired = payload;
            else
            {
                var prefix = InstallManifest.SafeRelative(component.Target) + "/";
                if (!relative.Replace('\\', '/').StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException($"关键路径不属于组件：{relative}");
                stagedRequired = InstallManifest.UnderRoot(staged, relative.Replace('\\', '/')[prefix.Length..]);
            }
            if (!File.Exists(stagedRequired)) throw new InvalidDataException($"组件 {component.Name} 缺少关键文件：{relative}");
        }

        progress.Report(new SetupProgress(component.Name, "正在提交"));
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        RejectLinks(root, target);
        if (File.Exists(target) || Directory.Exists(target)) throw new IOException($"提交前目标已出现：{component.Target}");
        if (File.Exists(payload)) File.Move(payload, target);
        else Directory.Move(payload, target);
        await state.CompleteAsync(root, component, cancellationToken);
        RejectLinks(root, temporary);
        Directory.Delete(temporary, recursive: true);
        progress.Report(new SetupProgress(component.Name, "已完成"));
    }

    private static bool HasExpectedFiles(string root, InstallComponent component) =>
        component.RequiredPaths.All(relative => File.Exists(InstallManifest.UnderRoot(root, relative)));

    private static long Measure(string path) => File.Exists(path)
        ? new FileInfo(path).Length
        : Directory.Exists(path) ? Directory.EnumerateFiles(path, "*", SearchOption.AllDirectories).Sum(file => new FileInfo(file).Length) : 0;

    internal static void RejectLinks(string root, string destination)
    {
        var basePath = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var finalPath = Path.GetFullPath(destination);
        if (finalPath != basePath && !finalPath.StartsWith(basePath + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("目标路径越界。");
        var current = basePath;
        if (Directory.Exists(current) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("安装根目录是链接。");
        foreach (var part in Path.GetRelativePath(basePath, finalPath).Split(Path.DirectorySeparatorChar))
        {
            if (part is "." or "") continue;
            current = Path.Combine(current, part);
            if ((File.Exists(current) || Directory.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException($"目标路径包含链接：{current}");
        }
    }

    private static string? ArchivePath(string staging, string name, int strip)
    {
        if (name.StartsWith('/') || name.StartsWith('\\') || name.Contains(':')) throw new InvalidDataException("归档包含绝对路径。");
        var parts = name.Replace('\\', '/').TrimEnd('/').Split('/');
        if (parts.Take(strip).Any(part => part is "" or "..") || parts.Skip(strip).Any(part => part is "" or "." or ".."))
            throw new InvalidDataException("归档路径越界。");
        if (parts.Length <= strip) return null;
        var relative = string.Join('/', parts.Skip(strip));
        var path = InstallManifest.UnderRoot(staging, relative);
        RejectLinks(staging, path);
        return path;
    }

    internal static void ExtractZip(string archive, string staging, int strip, CancellationToken cancellationToken)
    {
        using var zip = ZipFile.OpenRead(archive);
        foreach (var entry in zip.Entries)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var unixType = (entry.ExternalAttributes >> 16) & 0xF000;
            if (unixType == 0xA000) throw new InvalidDataException("归档包含符号链接。");
            var target = ArchivePath(staging, entry.FullName, strip);
            if (target is null) continue;
            if (entry.FullName.EndsWith('/')) { Directory.CreateDirectory(target); continue; }
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            using var input = entry.Open();
            using var output = new FileStream(target, FileMode.Create, FileAccess.Write, FileShare.None);
            input.CopyTo(output);
        }
    }

    private static void ExtractTar(string archive, string staging, int strip, CancellationToken cancellationToken)
    {
        using var file = File.OpenRead(archive);
        using var gzip = new GZipStream(file, CompressionMode.Decompress);
        using var tar = new TarReader(gzip);
        TarEntry? entry;
        while ((entry = tar.GetNextEntry()) is not null)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var target = ArchivePath(staging, entry.Name, strip);
            if (target is null) continue;
            if (entry.EntryType == TarEntryType.Directory) { Directory.CreateDirectory(target); continue; }
            if (entry.EntryType is not (TarEntryType.RegularFile or TarEntryType.V7RegularFile))
                throw new InvalidDataException($"归档包含不安全链接或特殊条目：{entry.Name}");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            using var output = new FileStream(target, FileMode.Create, FileAccess.Write, FileShare.None);
            entry.DataStream?.CopyTo(output);
        }
    }
}
