using System.IO;
using System.Text.Json;

namespace LiveSub.Launcher;

internal sealed class InstallState
{
    public string ProductVersion { get; set; } = "0.1.0";
    public string CoreVersion { get; set; } = "0.1.0";
    public bool BaseComplete { get; set; }
    public Dictionary<string, long> BaseKeySizes { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public string? CurrentUnit { get; set; }
    public Dictionary<string, string> OwnedTargets { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, CompletedComponent> Completed { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public bool ProbePassed { get; set; }
    public string? ProbeSummary { get; set; }

    public static InstallState? Load(string root)
    {
        var file = Path.Combine(root, "install-state.json");
        if (!File.Exists(file)) return null;
        var state = JsonSerializer.Deserialize<InstallState>(File.ReadAllText(file), InstallManifest.JsonOptions);
        if (state is not null)
        {
            state.OwnedTargets = new(state.OwnedTargets, StringComparer.OrdinalIgnoreCase);
            state.Completed = new(state.Completed, StringComparer.OrdinalIgnoreCase);
            state.BaseKeySizes = new(state.BaseKeySizes, StringComparer.OrdinalIgnoreCase);
        }
        return state;
    }

    public static bool IsReady(string root)
    {
        try
        {
            var state = Load(root);
            if (state is null || state.ProductVersion != "0.1.0" || !state.BaseComplete || !state.ProbePassed || !state.BaseKeysMatch(root) ||
                !File.Exists(Path.Combine(root, "LiveSub.exe")) ||
                !File.Exists(Path.Combine(root, ".venv", "Scripts", "python.exe")) ||
                !File.Exists(Path.Combine(root, "config.toml"))) return false;
            var manifest = InstallManifest.Load(root);
            return manifest.Components.Where(c => !c.Optional).All(c => state.IsComplete(root, c));
        }
        catch (Exception exc) when (exc is IOException or JsonException or UnauthorizedAccessException) { return false; }
    }

    public bool IsComplete(string root, InstallComponent component)
    {
        if (!Completed.TryGetValue(component.Id, out var record) || record.Identity != component.Identity()) return false;
        foreach (var (relative, size) in record.KeySizes)
        {
            var path = InstallManifest.UnderRoot(root, relative);
            if (!File.Exists(path) || new FileInfo(path).Length != size) return false;
        }
        return record.KeySizes.Count > 0;
    }

    public bool BaseKeysMatch(string root)
    {
        if (BaseKeySizes.Count == 0) return false;
        foreach (var (relative, size) in BaseKeySizes)
        {
            var path = InstallManifest.UnderRoot(root, relative);
            if (!File.Exists(path) || new FileInfo(path).Length != size) return false;
        }
        return true;
    }

    public async Task SaveAsync(string root, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(root);
        var target = Path.Combine(root, "install-state.json");
        var temp = target + ".new";
        await File.WriteAllTextAsync(temp, JsonSerializer.Serialize(this, InstallManifest.JsonOptions), cancellationToken);
        File.Move(temp, target, overwrite: true);
    }

    public async Task ClaimAsync(string root, string unit, string relative, CancellationToken cancellationToken)
    {
        relative = InstallManifest.SafeRelative(relative);
        var target = InstallManifest.UnderRoot(root, relative);
        if (OwnedTargets.TryGetValue(relative, out var owner))
        {
            if (owner != unit) throw new IOException($"目标已属于其他组件：{relative}");
        }
        else
        {
            if (File.Exists(target) || Directory.Exists(target)) throw new IOException($"发现未知文件或目录：{relative}。请选择新目录。");
            OwnedTargets.Add(relative, unit);
        }
        CurrentUnit = unit;
        await SaveAsync(root, cancellationToken);
    }

    public async Task CompleteAsync(string root, InstallComponent component, CancellationToken cancellationToken)
    {
        var record = new CompletedComponent { Identity = component.Identity() };
        foreach (var relative in component.RequiredPaths)
        {
            var path = InstallManifest.UnderRoot(root, relative);
            if (!File.Exists(path)) throw new IOException($"组件 {component.Name} 缺少关键文件：{relative}");
            record.KeySizes[relative] = new FileInfo(path).Length;
        }
        Completed[component.Id] = record;
        CurrentUnit = null;
        await SaveAsync(root, cancellationToken);
    }
}

internal sealed class CompletedComponent
{
    public string Identity { get; set; } = "";
    public Dictionary<string, long> KeySizes { get; set; } = new(StringComparer.OrdinalIgnoreCase);
}
