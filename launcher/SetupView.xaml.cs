using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;

namespace LiveSub.Launcher;

public partial class SetupView : UserControl
{
    private enum Step { Directory, Contents, Progress, Done }
    private readonly SetupCoordinator _coordinator = new();
    private InstallManifest? _manifest;
    private PrerequisiteReport? _check;
    private Step _step;
    private int _checkNumber;
    private int _completedComponents;
    private long _allDownloads;
    private string _root = "";

    public event Action<object, string>? Installed;
    public bool IsBusy => _coordinator.IsBusy;

    public SetupView() => InitializeComponent();

    public void Begin(string root)
    {
        _root = root;
        RootPath.Text = root;
        try
        {
            _manifest = InstallManifest.LoadBundled();
            foreach (var name in new[]
            {
                "🔒 LiveSub CLI 核心程序", "🔒 Python 环境与 AMD 依赖", "🔒 FFmpeg",
                "🔒 llama.cpp 与 ROCm", "🔒 Qwen3-ASR 1.7B", "🔒 Qwen3-ForcedAligner 0.6B",
                "🔒 Silero / Smart Turn", "🔒 SaT / 本地 tokenizer",
            }) RequiredList.Items.Add(name);
            _ = RefreshCheckAsync();
        }
        catch (Exception exc)
        {
            PrerequisiteError.Text = "组件清单不可用：" + exc.Message;
            PrimaryButton.IsEnabled = false;
        }
        ShowStep(Step.Directory);
    }

    private void ShowStep(Step step)
    {
        _step = step;
        DirectoryStep.Visibility = step == Step.Directory ? Visibility.Visible : Visibility.Collapsed;
        ContentsStep.Visibility = step == Step.Contents ? Visibility.Visible : Visibility.Collapsed;
        ProgressStep.Visibility = step == Step.Progress ? Visibility.Visible : Visibility.Collapsed;
        DoneStep.Visibility = step == Step.Done ? Visibility.Visible : Visibility.Collapsed;
        foreach (var (label, value) in new[] { (StepOne, Step.Directory), (StepTwo, Step.Contents), (StepThree, Step.Progress), (StepFour, Step.Done) })
        {
            label.Foreground = value == step ? (System.Windows.Media.Brush)FindResource("PrimaryBrush") : (System.Windows.Media.Brush)FindResource("InkBrush");
            label.FontWeight = value == step ? FontWeights.Bold : FontWeights.Normal;
        }
        Subtitle.Text = step switch
        {
            Step.Directory => "选择安装位置并检查运行条件",
            Step.Contents => "确认固定组件与可选翻译模型",
            Step.Progress => "下载、校验并安装组件",
            _ => "默认 7B GPU 环境验证通过",
        };
        SecondaryButton.Content = step switch { Step.Directory => "重新检查", Step.Contents => "上一步", Step.Progress => "取消安装", _ => "打开项目目录" };
        PrimaryButton.Content = step switch { Step.Directory => "下一步", Step.Contents => "开始安装", Step.Progress => "安装中", _ => "打开 LiveSub" };
        PrimaryButton.IsEnabled = step switch
        {
            Step.Directory => _manifest is not null && _check?.Passed == true,
            Step.Contents => _manifest is not null,
            Step.Progress => false,
            _ => true,
        };
        HelpButton.Visibility = step == Step.Done ? Visibility.Visible : Visibility.Collapsed;
        FooterNote.Text = step == Step.Progress ? "取消后保留已完成组件和未完成下载，下次可续装" : "英语 / 日语 → 简体中文外挂字幕";
    }

    private void Browse_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog { Title = "选择 LiveSub 安装目录", InitialDirectory = Directory.Exists(RootPath.Text) ? RootPath.Text : Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData) };
        if (dialog.ShowDialog() == true) RootPath.Text = dialog.FolderName;
    }

    private async void RootPath_Changed(object sender, TextChangedEventArgs e)
    {
        if (_step != Step.Directory || _manifest is null) return;
        await RefreshCheckAsync();
    }

    private async void OptionalModel_Changed(object sender, RoutedEventArgs e)
    {
        if (_manifest is null) return;
        UpdateSizes();
        if (_step == Step.Contents) await RefreshCheckAsync();
    }

    private async Task RefreshCheckAsync()
    {
        if (_manifest is null || string.IsNullOrWhiteSpace(RootPath.Text)) return;
        int current = ++_checkNumber;
        PrimaryButton.IsEnabled = false;
        PrerequisiteError.Text = "正在检查…";
        try
        {
            var result = await _coordinator.CheckAsync(RootPath.Text, _manifest, OptionalModel.IsChecked == true);
            if (current != _checkNumber) return;
            _check = result;
            PrerequisiteList.Items.Clear();
            foreach (var row in result.Rows) PrerequisiteList.Items.Add($"{(row.Passed ? "✓" : "✕")}  {row.Name}    {row.Detail}");
            PrerequisiteError.Text = result.Passed ? "前提检查通过" : "缺少前提时，请处理后重新检查。";
            if (_step == Step.Directory) PrimaryButton.IsEnabled = result.Passed;
            UpdateSizes();
        }
        catch (Exception exc)
        {
            if (current == _checkNumber) PrerequisiteError.Text = "检查失败：" + exc.Message;
        }
    }

    private void UpdateSizes()
    {
        if (_manifest is null) return;
        InstallState? state = null;
        string root;
        try { root = Path.GetFullPath(Environment.ExpandEnvironmentVariables(RootPath.Text)); }
        catch (Exception) { return; }
        try { state = InstallState.Load(root); } catch (Exception) { }
        var pending = _manifest.Components.Where(c => (!c.Optional || OptionalModel.IsChecked == true) && state?.IsComplete(root, c) != true).ToList();
        long download = (state?.BaseComplete == true ? 0 : _manifest.BaseDownloadSizeBytes) + pending.Sum(c => c.Sources.Sum(s => s.DownloadSizeBytes));
        long installed = (state?.BaseComplete == true ? 0 : _manifest.BaseInstalledSizeBytes) + pending.Sum(c => c.InstalledSizeBytes);
        DownloadSize.Text = $"剩余下载量：{GiB(download):F1} GiB";
        InstalledSize.Text = $"预计新增安装占用：{GiB(installed):F1} GiB";
        SpaceNeeded.Text = _check is null ? "" : $"保守空间需求：{GiB(_check.RequiredBytes):F1} GiB · 可用 {GiB(_check.AvailableBytes):F1} GiB";
        _allDownloads = download;
    }

    private async void SecondaryButton_Click(object sender, RoutedEventArgs e)
    {
        switch (_step)
        {
            case Step.Directory: await RefreshCheckAsync(); break;
            case Step.Contents: ShowStep(Step.Directory); break;
            case Step.Progress:
                if (!IsBusy) { ShowStep(Step.Contents); break; }
                SecondaryButton.IsEnabled = false;
                CurrentStage.Text = "正在取消并回收子进程…";
                await StopAsync();
                SecondaryButton.IsEnabled = true;
                break;
            case Step.Done:
                Process.Start(new ProcessStartInfo(_root) { UseShellExecute = true });
                break;
        }
    }

    private async void PrimaryButton_Click(object sender, RoutedEventArgs e)
    {
        switch (_step)
        {
            case Step.Directory:
                if (_check?.Passed != true) return;
                ShowStep(Step.Contents);
                UpdateSizes();
                break;
            case Step.Contents:
                if (_manifest is null) return;
                await RefreshCheckAsync();
                if (_check?.Passed != true) { ShowStep(Step.Directory); return; }
                await StartInstallAsync();
                break;
            case Step.Done: Installed?.Invoke(this, _root); break;
        }
    }

    private async Task StartInstallAsync()
    {
        if (_manifest is null) return;
        _root = Path.GetFullPath(Environment.ExpandEnvironmentVariables(RootPath.Text));
        _completedComponents = 0;
        ErrorCard.Visibility = Visibility.Collapsed;
        ProbeList.Items.Clear();
        ShowStep(Step.Progress);
        var progress = new Progress<SetupProgress>(item =>
        {
            CurrentComponent.Text = "当前组件：" + item.Component;
            CurrentStage.Text = item.Stage;
            ComponentProgress.IsIndeterminate = item.Stage != "正在下载";
            if (item.DownloadTotal > 0)
            {
                ComponentProgress.Maximum = item.DownloadTotal;
                ComponentProgress.Value = Math.Min(item.Downloaded, item.DownloadTotal);
                CurrentSpeed.Text = $"{GiB(item.Downloaded):F2} / {GiB(item.DownloadTotal):F2} GiB · {item.BytesPerSecond / 1024 / 1024:F1} MiB/s";
            }
            else CurrentSpeed.Text = "";
            if (item.Stage == "已完成") _completedComponents++;
            OverallProgress.Text = $"已完成 {_completedComponents} 个安装单元；预计剩余下载总量 {GiB(_allDownloads):F1} GiB";
        });
        try
        {
            await _coordinator.InstallAsync(_root, _manifest, OptionalModel.IsChecked == true, progress,
                (name, passed, detail) => Dispatcher.Invoke(() => ProbeList.Items.Add($"{(passed ? "✓" : "✕")}  {name}    {detail}")));
            InstalledPath.Text = _root;
            ConfigNote.Text = File.Exists(Path.Combine(_root, "config.toml.launcher-new"))
                ? "已有配置已保留；新模板保存为 config.toml.launcher-new，探针使用实际配置。" : "配置与安装记录已保存。";
            ShowStep(Step.Done);
        }
        catch (OperationCanceledException)
        {
            InstallError.Text = "已取消；已完成组件保留，当前组件未记完成。";
            ErrorCard.Visibility = Visibility.Visible;
            CurrentStage.Text = "安装已取消";
            SecondaryButton.Content = "返回安装内容";
        }
        catch (Exception exc)
        {
            InstallError.Text = exc.Message;
            ErrorCard.Visibility = Visibility.Visible;
            CurrentStage.Text = "安装失败";
            SecondaryButton.Content = "返回安装内容";
        }
    }

    public Task StopAsync() => _coordinator.StopAsync();
    private void HelpButton_Click(object sender, RoutedEventArgs e)
    {
        var file = Path.Combine(_root, "README.md");
        if (File.Exists(file)) Process.Start(new ProcessStartInfo(file) { UseShellExecute = true });
        else MessageBox.Show("使用说明文件不存在。", "LiveSub", MessageBoxButton.OK, MessageBoxImage.Warning);
    }
    private static double GiB(long value) => value / (1024.0 * 1024 * 1024);
}
