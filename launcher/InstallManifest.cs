using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace LiveSub.Launcher;

internal sealed class InstallManifest
{
    public long BaseDownloadSizeBytes { get; set; }
    public long BaseInstalledSizeBytes { get; set; }
    public long BasePeakExtraBytes { get; set; }
    public List<InstallComponent> Components { get; set; } = [];

    public static InstallManifest Load(string directory)
    {
        var file = Path.Combine(directory, "Resources", "components.json");
        if (!File.Exists(file)) throw new FileNotFoundException("安装目录缺少组件清单。", file);
        return Parse(File.ReadAllText(file));
    }

    public static InstallManifest LoadBundled()
    {
        using var stream = typeof(InstallManifest).Assembly.GetManifestResourceStream("LiveSub.Launcher.components.json")
            ?? throw new InvalidDataException("发布包缺少内嵌组件清单。");
        using var reader = new StreamReader(stream, Encoding.UTF8);
        return Parse(reader.ReadToEnd());
    }

    private static InstallManifest Parse(string json)
    {
        var manifest = JsonSerializer.Deserialize<InstallManifest>(json, JsonOptions)
            ?? throw new InvalidDataException("组件清单为空。");
        manifest.Validate();
        return manifest;
    }

    public void Validate()
    {
        if (BaseDownloadSizeBytes <= 0 || BaseInstalledSizeBytes <= 0 || BasePeakExtraBytes <= 0 || Components.Count == 0)
            throw new InvalidDataException("组件清单缺少真实的基础下载或空间数据。");
        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var targets = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var component in Components)
        {
            if (string.IsNullOrWhiteSpace(component.Id) || !ids.Add(component.Id)) throw new InvalidDataException("组件 ID 重复或为空。");
            if (string.IsNullOrWhiteSpace(component.Name) || component.InstalledSizeBytes <= 0 || component.Sources.Count == 0)
                throw new InvalidDataException($"组件 {component.Id} 缺少名称、大小或来源。");
            var target = SafeRelative(component.Target);
            if (!targets.Add(target)) throw new InvalidDataException($"组件目标重复：{target}");
            foreach (var required in component.RequiredPaths) SafeRelative(required);
            if (component.RequiredPaths.Count == 0) throw new InvalidDataException($"组件 {component.Id} 没有关键路径。");
            if (component.Sources.Count != 1 && component.Id != "rocm-runtime")
                throw new InvalidDataException($"组件 {component.Id} 只允许一个下载源。");
            foreach (var source in component.Sources)
            {
                if (!Uri.TryCreate(source.Url, UriKind.Absolute, out var url) || url.Scheme != Uri.UriSchemeHttps ||
                    source.DownloadSizeBytes <= 0 || source.Sha256.Length != 64 || !source.Sha256.All(Uri.IsHexDigit) ||
                    source.Archive is not ("none" or "zip" or "tar.gz") || source.StripComponents is < 0 or > 1)
                    throw new InvalidDataException($"组件 {component.Id} 的下载参数无效。");
                if (source.Archive == "none" && source.StripComponents != 0)
                    throw new InvalidDataException($"裸文件不能剥离目录：{component.Id}");
            }
        }
        foreach (var component in Components.Where(c => c.Sources[0].Archive != "none"))
        {
            var prefix = component.Target.TrimEnd('/', '\\') + "/";
            if (Components.Any(other => other.Id != component.Id && other.Target.Replace('\\', '/').StartsWith(prefix, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException($"归档目标与其他组件重叠：{component.Target}");
        }
    }

    public static string SafeRelative(string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative) || relative.Contains(':'))
            throw new InvalidDataException($"不是安全的相对路径：{relative}");
        var parts = relative.Replace('\\', '/').Split('/');
        if (parts.Any(part => part is "" or "." or "..")) throw new InvalidDataException($"路径越界：{relative}");
        return string.Join('/', parts);
    }

    public static string UnderRoot(string root, string relative)
    {
        var path = Path.GetFullPath(Path.Combine(root, SafeRelative(relative)));
        var normalized = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!path.StartsWith(normalized, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("目标路径越界。");
        return path;
    }

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        WriteIndented = true,
    };
}

internal sealed class InstallComponent
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string Target { get; set; } = "";
    public List<string> RequiredPaths { get; set; } = [];
    public long InstalledSizeBytes { get; set; }
    public bool Optional { get; set; }
    public List<DownloadSource> Sources { get; set; } = [];

    public string Identity()
    {
        var json = JsonSerializer.Serialize(this, InstallManifest.JsonOptions);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json)));
    }
}

internal sealed record DownloadSource
{
    public string Url { get; set; } = "";
    public long DownloadSizeBytes { get; set; }
    public string Sha256 { get; set; } = "";
    public string Archive { get; set; } = "";
    public int StripComponents { get; set; }
}
