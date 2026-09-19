using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace LiveSub.Launcher;

internal sealed record PrerequisiteRow(string Name, bool Passed, string Detail);
internal sealed record PrerequisiteReport(List<PrerequisiteRow> Rows, long RequiredBytes, long AvailableBytes)
{
    public bool Passed => Rows.All(row => row.Passed);
}

internal sealed class SetupCoordinator
{
    private CancellationTokenSource? _cancel;
    private Process? _activeProcess;
    private Task? _installTask;
    private string? _logPath;
    public bool IsBusy => _installTask is { IsCompleted: false };

    public async Task<PrerequisiteReport> CheckAsync(string root, InstallManifest manifest, bool includeOptional)
    {
        root = Path.GetFullPath(Environment.ExpandEnvironmentVariables(root));
        var rows = new List<PrerequisiteRow>();
        rows.Add(new("Windows 11 x64", OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000) && Environment.Is64BitOperatingSystem,
            RuntimeInformation.OSDescription));
        var python = await FindPythonAsync();
        rows.Add(new("Python 3.12 x64", python is not null, python ?? "未找到独立的 Python 3.12 x64"));
        bool vc = Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64", "Installed", null) is int installed && installed == 1;
        rows.Add(new("VC++ x64 运行库", vc, vc ? "已安装" : "请先安装 x64 运行库"));
        bool supportedGpu = false;
        string gpuDetail = "未识别目标显卡";
        try
        {
            var gpu = await RunSimpleAsync("powershell.exe", ["-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"], null, CancellationToken.None);
            supportedGpu = gpu.ExitCode == 0 && gpu.Output.Contains("AMD Radeon RX 9070 XT", StringComparison.OrdinalIgnoreCase);
            if (supportedGpu) gpuDetail = "已识别";
        }
        catch (Exception exc) when (exc is IOException or System.ComponentModel.Win32Exception)
        {
            gpuDetail = "显卡检查失败：" + exc.Message;
        }
        rows.Add(new("AMD RX 9070 XT", supportedGpu, gpuDetail));

        bool forbidden = IsWithin(root, Environment.GetFolderPath(Environment.SpecialFolder.Windows)) ||
            IsWithin(root, Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)) ||
            IsWithin(root, Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86));
        bool writable = false;
        try
        {
            if (!forbidden)
            {
                Directory.CreateDirectory(root);
                var probe = Path.Combine(root, ".livesub-write-" + Guid.NewGuid().ToString("N"));
                using (File.Create(probe)) { }
                File.Delete(probe);
                writable = true;
            }
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
        rows.Add(new("安装目录", writable, forbidden ? "不能安装到 Windows 或 Program Files" : writable ? "可写" : "目录不可写"));

        InstallState? state = null;
        try { state = InstallState.Load(root); }
        catch (Exception exc) when (exc is IOException or System.Text.Json.JsonException) { }
        bool compatible = state is null || state.ProductVersion == "0.1.0";
        rows.Add(new("安装记录", compatible, state is null ? "首次安装" : compatible ? "可续装" : "版本不同，请选择新目录"));

        var selected = manifest.Components.Where(c => !c.Optional || includeOptional).ToList();
        var pending = selected.Where(c => state?.IsComplete(root, c) != true).ToList();
        long baseSize = state?.BaseComplete == true ? 0 : manifest.BaseInstalledSizeBytes;
        long peak = Math.Max(state?.BaseComplete == true ? 0 : manifest.BasePeakExtraBytes,
            pending.Count == 0 ? 0 : pending.Max(c => c.Sources.Sum(s => s.DownloadSizeBytes)));
        long needed = baseSize + pending.Sum(c => c.InstalledSizeBytes) + peak + 2L * 1024 * 1024 * 1024;
        long available = 0;
        try { available = new DriveInfo(Path.GetPathRoot(root)!).AvailableFreeSpace; }
        catch (Exception exc) when (exc is IOException or ArgumentException) { }
        rows.Add(new("磁盘空间", available >= needed, $"可用 {GiB(available):F1} GiB · 需要 {GiB(needed):F1} GiB"));
        return new(rows, needed, available);
    }

    public Task InstallAsync(string root, InstallManifest manifest, bool includeOptional, IProgress<SetupProgress> progress, Action<string, bool, string> onProbe)
    {
        if (IsBusy) throw new InvalidOperationException("安装已在进行中。");
        _cancel = new CancellationTokenSource();
        _installTask = InstallCoreAsync(Path.GetFullPath(Environment.ExpandEnvironmentVariables(root)), manifest, includeOptional, progress, onProbe, _cancel.Token);
        return _installTask;
    }

    public async Task StopAsync()
    {
        _cancel?.Cancel();
        var process = _activeProcess;
        if (process is { HasExited: false }) process.Kill(entireProcessTree: true);
        if (_installTask is not null)
            try { await _installTask; } catch (OperationCanceledException) { } catch (Exception) { }
    }

    private async Task InstallCoreAsync(string root, InstallManifest manifest, bool includeOptional, IProgress<SetupProgress> progress, Action<string, bool, string> onProbe, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(root);
        Installer.RejectLinks(root, root);
        var state = InstallState.Load(root) ?? new InstallState();
        if (state.ProductVersion != "0.1.0") throw new IOException("已有不同版本安装记录，请选择新目录。");
        await state.SaveAsync(root, cancellationToken);
        var logs = Path.Combine(root, ".setup", "logs");
        Directory.CreateDirectory(logs);
        _logPath = Path.Combine(logs, $"install-{DateTime.Now:yyyyMMdd-HHmmss}.log");
        foreach (var old in Directory.GetFiles(logs, "install-*.log").OrderByDescending(File.GetCreationTimeUtc).Skip(5)) File.Delete(old);
        try
        {
            var check = await CheckAsync(root, manifest, includeOptional);
            if (!check.Passed) throw new IOException("安装前提尚未满足：" + string.Join("；", check.Rows.Where(r => !r.Passed).Select(r => r.Name + " " + r.Detail)));
            var python = await FindPythonAsync() ?? throw new IOException("未找到 Python 3.12 x64。");
            progress.Report(new SetupProgress("CLI 核心程序", "正在安装"));
            await LogAsync("开始：CLI 核心程序", cancellationToken);
            if (state.BaseComplete && !File.Exists(Path.Combine(root, ".venv", "Scripts", "python.exe")))
            {
                state.BaseComplete = false;
                state.ProbePassed = false;
                await state.SaveAsync(root, cancellationToken);
            }
            if (!state.BaseComplete || !state.BaseKeysMatch(root)) await InstallCoreFilesAsync(root, state, cancellationToken);
            await LogAsync("完成：CLI 核心程序", cancellationToken);
            if (!state.BaseComplete)
            {
                progress.Report(new SetupProgress("Python 环境与 AMD 依赖", "正在安装"));
                await LogAsync("开始：Python 环境与 AMD 依赖", cancellationToken);
                var venv = Path.Combine(root, ".venv");
                await state.ClaimAsync(root, "python-env", ".venv", cancellationToken);
                if (!File.Exists(Path.Combine(venv, "Scripts", "python.exe")))
                    await RunCheckedAsync(python, ["-m", "venv", venv], root, cancellationToken);
                var interpreter = Path.Combine(venv, "Scripts", "python.exe");
                await RunCheckedAsync(interpreter, ["-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], root, cancellationToken);
                await RunCheckedAsync(interpreter, ["-m", "pip", "install", "--extra-index-url", "https://stable.repo.amd.com/rocm/whl-next/", "-r", Path.Combine(root, "requirements.lock")], root, cancellationToken);
                await RunCheckedAsync(interpreter, ["-m", "pip", "install", "--no-deps", "--no-build-isolation", root], root, cancellationToken);
                await RunCheckedAsync(interpreter, ["-m", "pip", "check"], root, cancellationToken);
                state.BaseComplete = true;
                RecordBaseKeys(root, state);
                state.CurrentUnit = null;
                await state.SaveAsync(root, cancellationToken);
                await LogAsync("完成：Python 环境与 AMD 依赖", cancellationToken);
            }
            else if (!state.BaseKeysMatch(root))
            {
                RecordBaseKeys(root, state);
                await state.SaveAsync(root, cancellationToken);
            }
            var installer = new Installer();
            foreach (var component in manifest.Components.Where(c => !c.Optional || includeOptional))
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (state.IsComplete(root, component)) continue;
                await LogAsync("开始：" + component.Id, cancellationToken);
                await installer.InstallComponentAsync(root, component, state, progress, cancellationToken);
                await LogAsync("完成：" + component.Id, cancellationToken);
            }
            await InstallConfigAsync(root, state, cancellationToken);
            progress.Report(new SetupProgress("最小验证", "正在检查"));
            var probe = new ProbeRunner(RunCheckedAsync);
            var passed = await probe.RunAsync(root, includeOptional, onProbe, cancellationToken);
            state.ProbePassed = passed;
            state.ProbeSummary = passed ? "默认 7B GPU 探针通过" : "最小探针未通过";
            await state.SaveAsync(root, cancellationToken);
            if (!passed) throw new IOException("最小探针未通过；请查看失败项目，安装尚未完成。");
            progress.Report(new SetupProgress("安装", "已完成"));
        }
        catch (Exception exc)
        {
            await LogAsync($"失败：{state.CurrentUnit ?? "安装"}；{exc.GetType().Name}：{exc.Message}", CancellationToken.None);
            throw;
        }
    }

    private static async Task InstallCoreFilesAsync(string root, InstallState state, CancellationToken cancellationToken)
    {
        var sourceExe = Environment.ProcessPath ?? throw new IOException("无法确定启动器路径。");
        var targetExe = Path.Combine(root, "LiveSub.exe");
        if (!Path.GetFullPath(sourceExe).Equals(Path.GetFullPath(targetExe), StringComparison.OrdinalIgnoreCase))
        {
            await state.ClaimAsync(root, "core", "LiveSub.exe", cancellationToken);
            if (!File.Exists(targetExe)) File.Copy(sourceExe, targetExe);
            else if (new FileInfo(targetExe).Length != new FileInfo(sourceExe).Length)
                throw new IOException("已有 LiveSub.exe 与当前版本大小不同，不能覆盖运行文件；请选择新目录。");
        }
        var manifestTarget = Path.Combine(root, "Resources", "components.json");
        using var manifestStream = typeof(SetupCoordinator).Assembly.GetManifestResourceStream("LiveSub.Launcher.components.json")
            ?? throw new IOException("发布包缺少内嵌组件清单。");
        using var manifestData = new MemoryStream();
        await manifestStream.CopyToAsync(manifestData, cancellationToken);
        var manifestBytes = manifestData.ToArray();
        await state.ClaimAsync(root, "core", "Resources/components.json", cancellationToken);
        Directory.CreateDirectory(Path.GetDirectoryName(manifestTarget)!);
        if (!File.Exists(manifestTarget)) await File.WriteAllBytesAsync(manifestTarget, manifestBytes, cancellationToken);
        else if (!File.ReadAllBytes(manifestTarget).AsSpan().SequenceEqual(manifestBytes))
        {
            throw new IOException("已有组件清单与当前版本不同，请选择新目录。");
        }
        using var embedded = typeof(SetupCoordinator).Assembly.GetManifestResourceStream("LiveSub.Launcher.core-package.zip")
            ?? throw new IOException("发布包缺少内嵌 CLI 核心包，不能继续安装。");
        using var zip = new ZipArchive(embedded, ZipArchiveMode.Read);
        foreach (var entry in zip.Entries)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (entry.FullName.EndsWith('/')) continue;
            var relative = InstallManifest.SafeRelative(entry.FullName);
            if (relative.StartsWith(".venv/", StringComparison.OrdinalIgnoreCase) || relative.StartsWith("models/", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("核心包包含禁止嵌入的目录。");
            await state.ClaimAsync(root, "core", relative, cancellationToken);
            var target = InstallManifest.UnderRoot(root, relative);
            Installer.RejectLinks(root, target);
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            bool matches = false;
            if (File.Exists(target) && new FileInfo(target).Length == entry.Length)
            {
                await using var current = File.OpenRead(target);
                await using var input = entry.Open();
                var existingHash = await System.Security.Cryptography.SHA256.HashDataAsync(current, cancellationToken);
                var packagedHash = await System.Security.Cryptography.SHA256.HashDataAsync(input, cancellationToken);
                matches = existingHash.SequenceEqual(packagedHash);
            }
            if (!matches)
            {
                if (File.Exists(target)) File.Delete(target);
                await using var input = entry.Open();
                await using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, useAsync: true);
                await input.CopyToAsync(output, cancellationToken);
            }
        }
    }

    private static void RecordBaseKeys(string root, InstallState state)
    {
        foreach (var relative in new[] { "LiveSub.exe", "Resources/components.json", "requirements.lock", "config.toml.template", "src/subtitle_cli/ui_bridge.py", ".venv/Scripts/python.exe" })
        {
            var path = InstallManifest.UnderRoot(root, relative);
            if (!File.Exists(path)) throw new IOException($"基础环境缺少关键文件：{relative}");
            state.BaseKeySizes[relative] = new FileInfo(path).Length;
        }
    }

    private static async Task InstallConfigAsync(string root, InstallState state, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(Path.Combine(root, "outputs"));
        var template = Path.Combine(root, "config.toml.template");
        var current = Path.Combine(root, "config.toml");
        if (!File.Exists(template)) throw new IOException("核心包缺少配置模板。");
        if (!File.Exists(current))
        {
            await state.ClaimAsync(root, "config", "config.toml", cancellationToken);
            File.Copy(template, current);
        }
        else
        {
            var proposal = Path.Combine(root, "config.toml.launcher-new");
            if (!File.Exists(proposal))
            {
                await state.ClaimAsync(root, "config", "config.toml.launcher-new", cancellationToken);
                File.Copy(template, proposal);
            }
            else if (!state.OwnedTargets.TryGetValue("config.toml.launcher-new", out var owner) || owner != "config")
                throw new IOException("已有未知来源的 config.toml.launcher-new，请先另存该文件或选择新目录。");
        }
        state.CurrentUnit = null;
        await state.SaveAsync(root, cancellationToken);
    }

    private async Task RunCheckedAsync(string executable, IEnumerable<string> args, string root, CancellationToken cancellationToken)
    {
        var result = await RunSimpleAsync(executable, args, root, cancellationToken, process => _activeProcess = process);
        _activeProcess = null;
        if (result.ExitCode != 0) throw new IOException($"{Path.GetFileName(executable)} 退出码 {result.ExitCode}：{result.Output[^Math.Min(500, result.Output.Length)..]}");
    }

    private static async Task<string?> FindPythonAsync()
    {
        var candidates = new List<(string Exe, string[] Prefix)> { ("py.exe", ["-3.12"]), ("python.exe", []) };
        foreach (var hive in new[] { "HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER" })
        {
            var key = hive + @"\SOFTWARE\Python\PythonCore\3.12\InstallPath";
            var registered = Registry.GetValue(key, "ExecutablePath", null) as string;
            if (string.IsNullOrWhiteSpace(registered) && Registry.GetValue(key, null, null) is string directory)
                registered = Path.Combine(directory, "python.exe");
            if (!string.IsNullOrWhiteSpace(registered)) candidates.Add((registered, []));
        }
        foreach (var (executable, prefix) in candidates)
        {
            var args = prefix.Concat(["-c", "import sys;print(sys.executable if sys.version_info[:2]==(3,12) and sys.maxsize>2**32 else '')"]).ToArray();
            try
            {
                var result = await RunSimpleAsync(executable, args, null, CancellationToken.None);
                var path = result.Output.Trim();
                if (result.ExitCode == 0 && File.Exists(path) && !path.Contains(@"\.cache\codex-runtimes\", StringComparison.OrdinalIgnoreCase)) return path;
            }
            catch (Exception exc) when (exc is IOException or System.ComponentModel.Win32Exception) { }
        }
        return null;
    }

    private static async Task<(int ExitCode, string Output)> RunSimpleAsync(string executable, IEnumerable<string> args, string? root, CancellationToken cancellationToken, Action<Process>? started = null)
    {
        var info = new ProcessStartInfo(executable)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            WorkingDirectory = root ?? AppContext.BaseDirectory,
        };
        foreach (var arg in args) info.ArgumentList.Add(arg);
        using var process = Process.Start(info) ?? throw new IOException($"无法启动 {executable}");
        started?.Invoke(process);
        using var registration = cancellationToken.Register(() => { try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { } });
        var stdout = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var stderr = process.StandardError.ReadToEndAsync(cancellationToken);
        await process.WaitForExitAsync(cancellationToken);
        return (process.ExitCode, (await stdout) + (await stderr));
    }

    private async Task LogAsync(string line, CancellationToken cancellationToken)
    {
        if (_logPath is not null) await File.AppendAllTextAsync(_logPath, $"{DateTimeOffset.Now:O} {line}{Environment.NewLine}", cancellationToken);
    }

    private static bool IsWithin(string path, string parent) => !string.IsNullOrEmpty(parent) &&
        (path.Equals(parent, StringComparison.OrdinalIgnoreCase) || path.StartsWith(parent.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase));
    private static double GiB(long bytes) => bytes / (1024.0 * 1024 * 1024);
}
