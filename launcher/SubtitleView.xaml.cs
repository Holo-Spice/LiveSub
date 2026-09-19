using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Win32;

namespace LiveSub.Launcher;

public partial class SubtitleView : UserControl
{
    /// <summary>Startup stages in the order the bridge reports them, for the n/8 counter.</summary>
    private static readonly string[] StartupStageOrder = { "preflight", "model_load", "model_deps", "model_asr", "model_aligner", "model_ready", "translator_start", "translator_ready" };

    /// <summary>Share of the startup bar each stage has already finished.</summary>
    private static readonly Dictionary<string, double> StartupProgress = new()
    {
        ["preflight"] = 0, ["model_load"] = 5, ["model_deps"] = 10, ["model_asr"] = 35,
        ["model_aligner"] = 65, ["model_warmup"] = 75, ["model_ready"] = 80, ["translator_start"] = 85, ["translator_ready"] = 100,
    };

    /// <summary>Model-load items reported by subtitle_cli, in the words a user recognises.</summary>
    private static readonly Dictionary<string, string> LoadStepNames = new()
    {
        ["onnxruntime"] = "本地推理组件", ["torch"] = "显卡计算库", ["torch_cuda"] = "显卡接口",
        ["torch_devices"] = "显卡列表", ["silero_vad"] = "静音检测组件", ["silero_vad_model"] = "静音检测模型",
        ["smart_turn"] = "断句模型", ["transformers"] = "文本处理组件", ["whisper_features"] = "音频特征提取",
        ["wtpsplit"] = "分句组件", ["sat_model"] = "分句模型", ["asr_processor"] = "语音识别配置",
        ["asr_weights"] = "语音识别权重", ["asr_to_gpu"] = "语音识别送入显存", ["aligner_processor"] = "时间对齐配置",
        ["aligner_weights"] = "时间对齐权重", ["aligner_to_gpu"] = "时间对齐送入显存",
        ["warmup"] = "预热显卡算子",
    };

    private readonly TaskRunner _runner = new();
    private readonly ConcurrentQueue<UiMessage> _pending = new();
    private readonly ObservableCollection<SubtitleRow> _rows = new();
    private readonly DispatcherTimer _refresh = new() { Interval = TimeSpan.FromMilliseconds(250) };
    private readonly Stopwatch _startupClock = new();
    private SubtitleOverlayWindow? _overlay;
    private string _root = "";
    private string _mode = "offline";
    private string _stage = "";
    private string? _loadStep;
    private double? _loadStepSeconds;
    private bool _startupEnded;
    private int _saved;
    private long _capturedSeconds;
    /// <summary>The rate the CLI opened the capture device with, e.g. "96000 Hz / 2 ch".</summary>
    private string _captureSuffix = "";
    private long _latestMediaMs;
    private bool _busy;
    private bool _stopping;
    private bool _runProgress;
    private double? _processedSeconds;
    private double? _totalSeconds;
    private double? _etaSeconds;
    private double? _speed;

    public SubtitleView()
    {
        InitializeComponent();
        SubtitleList.ItemsSource = _rows;
        _refresh.Tick += (_, _) =>
        {
            FlushMessages();
            // The elapsed clock has to keep moving while a stage runs, or a slow load looks
            // exactly like a hang again.
            if (_busy && StartupBar.Visibility == Visibility.Visible) TaskDetail.Text = StartupText();
        };
        _refresh.Start();
    }

    public bool IsRunning => _busy;

    public void Begin(string root, bool existing = false)
    {
        _root = Path.GetFullPath(root);
        try
        {
            // The two modes write into their own folder, so a live session never blocks or
            // overwrites the subtitle of a video.
            Directory.CreateDirectory(Path.Combine(_root, "outputs"));
            Directory.CreateDirectory(Path.Combine(_root, "outputs", "live"));
            Directory.CreateDirectory(Path.Combine(_root, "outputs", "offline"));
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
        if (existing)
        {
            HeaderStatus.Text = "● 正在使用现有环境，待启动检查";
            SmallModel.IsEnabled = File.Exists(ModelPath("1.8b"));
        }
        else
        {
            var state = InstallState.Load(_root);
            HeaderStatus.Text = state?.ProbePassed == true ? "● 环境就绪" : "● 已安装，待启动检查";
            var optional = InstallManifest.Load(_root).Components.Single(c => c.Id == "hymt-1-8b");
            SmallModel.IsEnabled = state?.IsComplete(_root, optional) == true;
        }
        // 1.8B is optional: the entry stays disabled until the file is really installed.
        SmallModel.IsEnabled = existing ? File.Exists(ModelPath("1.8b")) : InstallState.Load(_root)?.IsComplete(_root, InstallManifest.Load(_root).Components.Single(c => c.Id == "hymt-1-8b")) == true;
        ModelHint.Text = SmallModel.IsEnabled ? "1.8B 已安装" : "1.8B 已安装时可选";
        MtPrompt.Text = "";
        PromptHint.Text = $"留空 = 内置默认：{DefaultMtPrompt}";
        SetMode("offline");
    }

    /// <summary>The instruction the translation model gets when the box is left empty.</summary>
    private const string DefaultMtPrompt = "你是专业的影视与动画字幕译者。请将下面的对白翻译为自然、简洁的简体中文，适合电影、电视剧和动画字幕。准确保留原意、人物语气、情绪、礼貌程度和人物关系，避免逐字硬译或擅自补充；人名、地名和作品术语优先使用通行中文译名，无法确定时保留原文。只输出译文，不要解释、注音、括号说明或复述原文；不得输出繁体字。";

    private void PromptDefault_Click(object sender, RoutedEventArgs e)
    {
        MtPrompt.Text = DefaultMtPrompt;
        PromptHint.Text = "已填入默认文本；清空则回到同一份默认值";
    }

    private void SetMode(string mode)
    {
        if (_busy) return;
        _mode = mode;
        VideoCard.Visibility = mode == "offline" ? Visibility.Visible : Visibility.Collapsed;
        AudioCard.Visibility = mode == "live" ? Visibility.Visible : Visibility.Collapsed;
        OfflineMode.Style = mode == "offline" ? (Style)FindResource("PrimaryButton") : null;
        LiveMode.Style = mode == "live" ? (Style)FindResource("PrimaryButton") : null;
        HeroTitle.Text = mode == "offline" ? "开始生成中文字幕" : "采集系统音频并生成字幕";
        ActionButton.Content = mode == "offline" ? "开始生成" : "开始采集";
        // The two modes keep their subtitles apart: a live session and a video would otherwise
        // both land in outputs/ and the next run would refuse the name as already used.
        OutputPath.Text = SuggestedOutput();
        // Switching mode must not leave the other mode's subtitles on screen pretending to be
        // this mode's result.
        _saved = 0;
        _rows.Clear();
        SavedTitle.Text = "已保存字幕 · 0 条";
        PreviewHint.Visibility = Visibility.Collapsed;
        if (mode == "live") _ = RefreshDevicesAsync();
        ValidateInput();
    }

    private string SuggestedOutput()
    {
        var folder = Path.Combine(_root, "outputs", _mode);
        return _mode == "live"
            ? Path.Combine(folder, $"live-{DateTime.Now:yyyyMMdd-HHmmss}.zh.srt")
            : Path.Combine(folder, "subtitle.zh.srt");
    }

    private void OfflineMode_Click(object sender, RoutedEventArgs e) => SetMode("offline");
    private void LiveMode_Click(object sender, RoutedEventArgs e) => SetMode("live");

    private void ChooseVideo_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Title = "选择视频", Filter = "视频文件|*.mp4;*.mkv;*.mov;*.webm;*.avi|所有文件|*.*", CheckFileExists = true };
        if (dialog.ShowDialog() != true) return;
        VideoPath.Text = dialog.FileName;
        OutputPath.Text = Path.Combine(_root, "outputs", "offline", Path.GetFileNameWithoutExtension(dialog.FileName) + ".zh.srt");
    }

    private void ChooseOutput_Click(object sender, RoutedEventArgs e)
    {
        var folder = Path.Combine(_root, "outputs", _mode);
        Directory.CreateDirectory(folder);
        var dialog = new SaveFileDialog
        {
            Title = _mode == "live" ? "保存实时字幕" : "保存外挂字幕",
            Filter = "SRT 字幕|*.srt",
            DefaultExt = ".srt",
            AddExtension = true,
            OverwritePrompt = false,
            InitialDirectory = folder,
            FileName = _mode == "live" ? $"live-{DateTime.Now:yyyyMMdd-HHmmss}.zh.srt" : "subtitle.zh.srt",
        };
        if (dialog.ShowDialog() == true) OutputPath.Text = dialog.FileName;
    }

    private async void RefreshDevices_Click(object sender, RoutedEventArgs e) => await RefreshDevicesAsync();

    private async Task RefreshDevicesAsync()
    {
        if (_busy) return;
        AudioDevices.Items.Clear();
        AudioHint.Text = "正在查找 DirectShow 音频设备…";
        RefreshDevicesButton.IsEnabled = false;
        try
        {
            var ffmpeg = ConfigPath("ffmpeg");
            if (!File.Exists(ffmpeg)) throw new FileNotFoundException("未找到项目 FFmpeg", ffmpeg);
            var info = new ProcessStartInfo(ffmpeg)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                StandardErrorEncoding = Encoding.UTF8,
            };
            foreach (var part in new[] { "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy" }) info.ArgumentList.Add(part);
            using var process = Process.Start(info) ?? throw new IOException("FFmpeg 未能启动。");
            var stderr = process.StandardError.ReadToEndAsync();
            var stdout = process.StandardOutput.ReadToEndAsync();
            try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10)); }
            catch (TimeoutException) { process.Kill(entireProcessTree: true); throw new IOException("设备枚举超时。"); }
            var listing = await stderr;
            await stdout;
            foreach (Match match in Regex.Matches(listing, "\"([^\"]+)\" \\(audio\\)"))
                AudioDevices.Items.Add(match.Groups[1].Value);
            AudioHint.Text = AudioDevices.Items.Count == 0
                ? "未找到可用音频设备，请按部署指南配置回环设备后刷新"
                : "请选择回环设备；不会自动选择麦克风";
        }
        catch (Exception exc) { AudioHint.Text = $"设备查找失败：{exc.Message}"; }
        finally { RefreshDevicesButton.IsEnabled = true; ValidateInput(); }
    }

    private void Input_Changed(object sender, EventArgs e) => ValidateInput();

    private void ValidateInput(bool updateMessage = true)
    {
        if (ActionButton is null || _busy) return;
        string? error = null;
        if (string.IsNullOrEmpty(_root)) return;
        if (_mode == "offline" && !File.Exists(VideoPath.Text)) error = "请选择存在的本地视频";
        if (_mode == "live" && AudioDevices.SelectedItem is null) error = "请选择可用的系统音频设备";
        if (SourceLanguage.SelectedItem is not ComboBoxItem || Model.SelectedItem is not ComboBoxItem || Device.SelectedItem is not ComboBoxItem)
            error = "请选择源语言、模型和设备";
        var model = (Model.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "7b";
        if (!File.Exists(ModelPath(model))) error = $"翻译模型 {model} 未安装";
        if (string.IsNullOrWhiteSpace(OutputPath.Text)) error = "请选择 .srt 保存位置";
        else
        {
            try
            {
                var output = AbsoluteOutput();
                var parent = Path.GetDirectoryName(output)!;
                if (!output.EndsWith(".srt", StringComparison.OrdinalIgnoreCase)) error = "保存文件必须以 .srt 结尾";
                else if (File.Exists(output) || File.Exists(Path.ChangeExtension(output, ".jsonl"))) error = "输出文件已存在，请更换文件名";
                else
                {
                    // The mode folders under outputs/ are created on demand: the two modes
                    // keep their subtitles apart, so the folder cannot be user-supplied only.
                    if (IsInsideRoot(parent)) Directory.CreateDirectory(parent);
                    if (!Directory.Exists(parent)) error = "输出目录不存在，请选择已有目录";
                    else
                    {
                        var probe = Path.Combine(parent, ".livesub-write-" + Guid.NewGuid().ToString("N"));
                        using (File.Create(probe)) { }
                        File.Delete(probe);
                    }
                }
            }
            catch (Exception exc) when (exc is IOException or UnauthorizedAccessException or ArgumentException)
            { error = "输出目录不可写或路径无效"; }
        }
        ActionButton.IsEnabled = error is null;
        if (updateMessage)
        {
            TaskStatus.Text = error ?? "准备就绪，可以开始";
            TaskDetail.Text = error is null
                ? $"生成简体中文外挂 SRT 与同名 JSONL · 保存在 outputs\\{_mode}"
                : "";
        }
    }

    /// <summary>A multi-line box becomes one line: the CLI takes a single argument.</summary>
    private static string Join(string text) => string.Join(" ", text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));

    private bool IsInsideRoot(string path)
    {
        var root = Path.GetFullPath(_root).TrimEnd('\\') + "\\";
        return Path.GetFullPath(path).StartsWith(root, StringComparison.OrdinalIgnoreCase);
    }

    private string AbsoluteOutput() => Path.GetFullPath(Path.IsPathRooted(OutputPath.Text) ? OutputPath.Text : Path.Combine(_root, OutputPath.Text));
    private string ModelPath(string model) => ConfigPath(model == "1.8b" ? "hymt_1_8b" : "hymt_7b");

    private string ConfigPath(string key)
    {
        try
        {
            bool paths = false;
            foreach (var line in File.ReadLines(Path.Combine(_root, "config.toml")))
            {
                var trimmed = line.Trim();
                if (trimmed.StartsWith('[')) { paths = trimmed == "[paths]"; continue; }
                if (!paths) continue;
                var match = Regex.Match(trimmed, "^" + Regex.Escape(key) + "\\s*=\\s*\"([^\"]+)\"");
                if (match.Success) return Path.GetFullPath(Path.Combine(_root, match.Groups[1].Value));
            }
        }
        catch (Exception) { }
        return Path.Combine(_root, "missing", key);
    }

    private async void ActionButton_Click(object sender, RoutedEventArgs e)
    {
        if (_busy) { await StopAsync(); return; }
        ValidateInput();
        if (!ActionButton.IsEnabled) return;
        _saved = 0;
        _capturedSeconds = 0;
        _captureSuffix = "";
        _latestMediaMs = 0;
        _rows.Clear();
        _stage = "";
        _loadStep = null;
        _loadStepSeconds = null;
        _startupEnded = false;
        _runProgress = false;
        _processedSeconds = null;
        _totalSeconds = null;
        _etaSeconds = null;
        _speed = null;
        _startupClock.Restart();
        StartupBar.Value = 0;
        StartupBar.Visibility = Visibility.Visible;
        RunBar.Value = 0;
        RunBar.IsIndeterminate = false;
        RunBar.Visibility = Visibility.Collapsed;
        SavedTitle.Text = "已保存字幕 · 0 条";
        PreviewHint.Visibility = Visibility.Collapsed;
        _stopping = false;
        SetBusy(true);
        TaskStatus.Text = "正在启动字幕任务";
        TaskDetail.Text = Path.ChangeExtension(AbsoluteOutput(), ".jsonl");
        if (_mode == "live") Overlay().ShowHint("正在启动，等待语音…");
        try
        {
            var options = new TaskOptions(_root, _mode,
                _mode == "offline" ? VideoPath.Text : AudioDevices.SelectedItem!.ToString()!,
                (SourceLanguage.SelectedItem as ComboBoxItem)!.Tag.ToString()!,
                (Model.SelectedItem as ComboBoxItem)!.Tag.ToString()!,
                (Device.SelectedItem as ComboBoxItem)!.Tag.ToString()!, AbsoluteOutput(),
                Join(Hotwords.Text), Join(MtPrompt.Text));
            var outcome = await _runner.Start(options, _pending.Enqueue);
            FlushMessages();
            TaskStatus.Text = outcome.Result switch
            {
                // "No speech" is the right wording for a video with no dialogue, but in live
                // mode it usually means the wrong device or audio that had not started yet.
                "completed" when _saved == 0 && _mode == "live" => "所选设备没有采集到语音；请确认它正在播放声音",
                "completed" when _saved == 0 => "未检测到可生成字幕的语音",
                "completed" => $"生成完成 · 共 {_saved} 条",
                "stopped" => $"已停止，保留已保存字幕 · {_saved} 条",
                "incomplete" => "字幕不完整：" + outcome.Message,
                _ => "字幕不完整：" + outcome.Message,
            };
            TaskDetail.Text = outcome.Stage is not null
                ? $"阶段：{outcome.Stage} · 诊断：{Path.GetFileName(Path.ChangeExtension(options.Output, ".jsonl"))}"
                : outcome.Result == "completed" && _processedSeconds is double done
                    ? $"已处理 {Duration(done)} 音频 · {Path.GetFileName(options.Output)}"
                    : Path.GetFileName(options.Output);
        }
        catch (Exception exc) { TaskStatus.Text = "字幕不完整：" + exc.Message; }
        finally { SetBusy(false); }
    }

    private void SetBusy(bool busy)
    {
        _busy = busy;
        if (!busy)
        {
            _startupClock.Stop();
            StartupBar.Visibility = Visibility.Collapsed;
            RunBar.Visibility = Visibility.Collapsed;
            RunBar.IsIndeterminate = false;
            CloseOverlay();
        }
        OfflineMode.IsEnabled = LiveMode.IsEnabled = !busy;
        VideoCard.IsEnabled = AudioCard.IsEnabled = ParameterCard.IsEnabled = OutputCard.IsEnabled = HotwordCard.IsEnabled = PromptCard.IsEnabled = !busy;
        ActionButton.Content = busy ? (_mode == "live" ? "停止采集" : "停止生成") : (_mode == "live" ? "开始采集" : "开始生成");
        ActionButton.IsEnabled = busy && !_stopping;
        if (!busy) ValidateInput(updateMessage: false);
    }

    /// <summary>The live subtitle strip over other applications, created on first use.</summary>
    private SubtitleOverlayWindow Overlay()
    {
        if (_overlay is null) _overlay = new SubtitleOverlayWindow(_root);
        return _overlay;
    }

    private void CloseOverlay()
    {
        if (_overlay is null) return;
        try { _overlay.Close(); }
        catch (InvalidOperationException) { }
        _overlay = null;
    }

    public async Task StopAsync()
    {
        if (!_busy || _stopping) return;
        _stopping = true;
        ActionButton.IsEnabled = false;
        TaskStatus.Text = _mode == "live" ? "正在处理已采集内容" : "正在停止生成";
        await _runner.StopAsync();
    }

    private void FlushMessages()
    {
        // Live lines are collected first and handed over as one batch. A single utterance is
        // written as several lines at once, and the overlay must see them together to time them
        // by their own duration: sending them one at a time is exactly what made a long line
        // disappear behind the short phrase that followed it.
        var live = new List<(string Text, long StartMs, long EndMs, bool Keep)>();
        while (_pending.TryDequeue(out var message))
        {
            if (message.Type == "subtitle")
            {
                _saved = message.Index!.Value;
                _rows.Add(new SubtitleRow($"{_saved:00}    {FormatMs(message.StartMs!.Value)} → {FormatMs(message.EndMs!.Value)}", message.Text!));
                while (_rows.Count > 200) _rows.RemoveAt(0);
                SavedTitle.Text = $"已保存字幕 · {_saved} 条 · {_mode}";
                PreviewHint.Visibility = _saved > 200 ? Visibility.Visible : Visibility.Collapsed;
                PreviewHint.Text = "仅展示最近 200 条，完整结果保存在 SRT";
                if (_mode == "live") live.Add((message.Text!, message.StartMs!.Value, message.EndMs!.Value, false));
                if (message.EndMs!.Value > _latestMediaMs) _latestMediaMs = message.EndMs!.Value;
            }
            else if (message.Type == "progress" && !_stopping)
            {
                ShowRunProgress(message);
            }
            else if (message.Type == "listening" && !_stopping)
            {
                var waited = TimeSpan.FromSeconds(message.WaitSeconds ?? 0);
                TaskStatus.Text = "正在等待语音";
                TaskDetail.Text = message.RemainingSeconds is double left and > 0
                    ? $"已等待 {waited:mm\\:ss}，仍未检测到语音；请确认所选的系统音频设备正在播放声音（{left:F0} 秒后自动停止）"
                    : $"已等待 {waited:mm\\:ss}，仍未检测到语音；请确认所选的系统音频设备正在播放声音";
            }
            else if (message.Type == "status" && !_stopping)
            {
                // The capture rate arrives with the first block, before any subtitle exists, so the
                // detail line can name the real device format instead of an assumed one.
                if (message.CaptureFormat is not null)
                {
                    _captureSuffix = $"  |  采集 {message.CaptureFormat}";
                    if (TaskStatus.Text == "采集中" || message.CapturedSeconds is not null)
                    {
                        TaskDetail.Text = $"采集时长 {TimeSpan.FromSeconds(_capturedSeconds):hh\\:mm\\:ss}  |  已保存 {_saved} 条{_captureSuffix}";
                    }
                }
                if (message.CapturedSeconds is int seconds)
                {
                    _capturedSeconds = seconds;
                    TaskDetail.Text = $"采集时长 {TimeSpan.FromSeconds(seconds):hh\\:mm\\:ss}  |  已保存 {_saved} 条{_captureSuffix}";
                    StartupBar.Visibility = Visibility.Collapsed;
                }
                if (message.Seconds is double spent) _loadStepSeconds = spent;
                if (message.Detail is not null) _loadStep = message.Detail;
                if (message.Stage is not null && message.Stage != _stage)
                {
                    _stage = message.Stage;
                    _loadStep = null;
                    _loadStepSeconds = null;
                    TaskStatus.Text = StageText(_stage);
                }
                UpdateStartupBar();
            }
        }
        if (live.Count > 0)
        {
            // Only the last line is allowed to stay after the utterance ends; it is the one the
            // user keeps looking at while the speaker pauses.
            live[^1] = live[^1] with { Keep = true };
            // Where the utterance starts on the audio timeline relative to this moment. The
            // capture clock is the position the audio has reached; the first line's start says
            // how far back the utterance began. Handing this to the overlay lets the lines whose
            // audio the listener has already heard be dropped and the rest be paced by the
            // speech, instead of dumping the whole utterance at once.
            long audioMs = Math.Max(_capturedSeconds * 1000, _latestMediaMs);
            Overlay().Show(live, Math.Max(0, audioMs - live[0].StartMs));
        }
    }

    /// <summary>Offline speed and remaining time, both from what the CLI has measured.</summary>
    private void ShowRunProgress(UiMessage message)
    {
        _runProgress = true;
        _processedSeconds = message.ProcessedSeconds;
        _totalSeconds = message.TotalSeconds;
        _etaSeconds = message.EtaSeconds;
        _speed = message.Speed;
        StartupBar.Visibility = Visibility.Collapsed;
        RunBar.Visibility = Visibility.Visible;
        TaskStatus.Text = "正在转写并翻译";
        var processed = _processedSeconds ?? 0;
        if (_totalSeconds is double total and > 0)
        {
            RunBar.IsIndeterminate = false;
            RunBar.Value = Math.Min(100, processed / total * 100);
            var parts = new StringBuilder($"{FormatMs((long)(processed * 1000))} / {FormatMs((long)(total * 1000))}（{processed / total * 100:F0}%）");
            if (_speed is double speed and > 0)
            {
                parts.Append($" · {speed:F2}× 于原速");
                parts.Append(_etaSeconds is double eta ? $" · 预计剩余 {Duration(eta)}" : " · 正在估算剩余时间");
            }
            else
            {
                parts.Append(" · 正在测量速度");
            }
            if (message.SegmentsDone is int done) parts.Append($" · 已完成 {done} 段");
            TaskDetail.Text = parts.ToString();
        }
        else
        {
            RunBar.IsIndeterminate = true;
            TaskDetail.Text = $"已处理 {Duration(processed)} 音频 · 无法读取视频时长，仅显示已处理量";
        }
    }

    private static string Duration(double seconds)
    {
        var span = TimeSpan.FromSeconds(Math.Max(0, seconds));
        return span.TotalHours >= 1 ? $"{(int)span.TotalHours} 小时 {span.Minutes} 分" : span.TotalMinutes >= 1 ? $"{(int)span.TotalMinutes} 分 {span.Seconds} 秒" : $"{span.Seconds} 秒";
    }

    /// <summary>The model load has no measurable percentage, but every stage and every item
    /// inside it is a real, reported event, so the bar advances on those and the text always
    /// shows how long the current one has been running.</summary>
    private void UpdateStartupBar()
    {
        // Once the run is reporting real processed audio, the startup bar has nothing left to
        // say: the pipeline stages that follow are per-utterance and would fight the file bar.
        if (_runProgress) return;
        if (!StartupProgress.TryGetValue(_stage, out double reached))
        {
            if (_startupEnded) return;
            _startupEnded = true;
            StartupBar.Value = 100;
            StartupBar.Visibility = Visibility.Collapsed;
            return;
        }
        StartupBar.Value = reached;
        if (_busy) StartupBar.Visibility = Visibility.Visible;
        TaskDetail.Text = StartupText();
    }

    private string StartupText()
    {
        var text = new StringBuilder(TaskStatus.Text);
        if (_loadStep is not null)
        {
            text.Append(" · ").Append(LoadStepNames.TryGetValue(_loadStep, out var name) ? name : _loadStep);
            text.Append(_loadStepSeconds is double spent ? $"（已完成 {spent:F1} 秒）" : "（进行中）");
        }
        text.Append($" · 第 {Math.Max(1, Array.IndexOf(StartupStageOrder, _stage) + 1)}/{StartupStageOrder.Length} 步");
        text.Append($" · 已用 {_startupClock.Elapsed.TotalSeconds:F0} 秒");
        if (_startupClock.Elapsed.TotalSeconds >= 45 && _stage != "translator_ready") text.Append("（较慢，仍在运行）");
        return text.ToString();
    }

    private static string StageText(string stage) => stage switch
    {
        "capturing" => "采集中",
        "listening" => "正在等待语音",
        "vad" or "smart_turn" => "等待下一句完整表达",
        "asr" => "正在识别语音",
        "sat" or "aligner" or "sentence_mapping" => "正在对齐完整句子",
        "translation" => "正在翻译",
        "subtitle_write" => "正在保存字幕",
        "preflight" => "正在检查运行环境",
        "model_load" or "model_deps" => "正在加载本地组件",
        "model_asr" => "正在加载语音识别模型",
        "model_aligner" => "正在加载时间对齐模型",
        "model_warmup" => "正在预热显卡算子",
        "model_ready" => "语音模型已就绪",
        "translator_start" => "正在启动翻译模型",
        "translator_ready" => "翻译模型已就绪",
        _ => stage,
    };

    private static string FormatMs(long value) => $"{value / 3_600_000:00}:{value / 60_000 % 60:00}:{value / 1_000 % 60:00},{value % 1_000:000}";
    private sealed record SubtitleRow(string Header, string Text);
}
