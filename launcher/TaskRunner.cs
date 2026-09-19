using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace LiveSub.Launcher;

internal sealed record TaskOptions(string Root, string Mode, string Input, string SourceLanguage, string Model, string Device, string Output, string Hotwords = "", string MtPrompt = "");
internal sealed record UiMessage(
    string Type,
    string? Stage = null,
    int? CapturedSeconds = null,
    int? Index = null,
    long? StartMs = null,
    long? EndMs = null,
    string? Text = null,
    string? Result = null,
    string? Message = null,
    string? Detail = null,
    double? Seconds = null,
    double? ProcessedSeconds = null,
    double? TotalSeconds = null,
    double? EtaSeconds = null,
    double? Speed = null,
    double? DecodedSeconds = null,
    int? SegmentsDone = null,
    int? SegmentsOpen = null,
    double? WaitSeconds = null,
    double? TimeoutSeconds = null,
    double? RemainingSeconds = null);
internal sealed record TaskOutcome(string Result, string Message, string? Stage = null);

internal sealed class TaskRunner
{
    private readonly TimeSpan _stopTimeout;
    private readonly object _gate = new();
    private readonly Queue<string> _stderrTail = new();
    private Process? _process;
    private Task<TaskOutcome>? _running;
    private bool _stopSent;
    private bool _timedOut;

    public TaskRunner(TimeSpan? stopTimeout = null) => _stopTimeout = stopTimeout ?? TimeSpan.FromSeconds(60);

    public bool IsRunning { get { lock (_gate) return _running is { IsCompleted: false }; } }

    public Task<TaskOutcome> Start(TaskOptions options, Action<UiMessage> onMessage)
    {
        lock (_gate)
        {
            if (_running is { IsCompleted: false })
                throw new InvalidOperationException("已有字幕任务正在运行。");
            _stopSent = false;
            _timedOut = false;
            _stderrTail.Clear();
            _running = RunAsync(options, onMessage);
            return _running;
        }
    }

    public async Task<TaskOutcome?> StopAsync()
    {
        Task<TaskOutcome>? running;
        Process? process;
        bool send;
        lock (_gate)
        {
            running = _running;
            process = _process;
            send = running is { IsCompleted: false } && !_stopSent;
            if (send) _stopSent = true;
        }
        if (running is null) return null;
        if (send && process is { HasExited: false })
        {
            try
            {
                await process.StandardInput.WriteLineAsync("{\"command\":\"stop\"}");
                await process.StandardInput.FlushAsync();
            }
            catch (IOException) { }
            catch (InvalidOperationException) { }
        }
        try
        {
            return await running.WaitAsync(_stopTimeout);
        }
        catch (TimeoutException)
        {
            _timedOut = true;
            if (process is { HasExited: false }) process.Kill(entireProcessTree: true);
            try { await running.WaitAsync(TimeSpan.FromSeconds(10)); } catch (TimeoutException) { }
            return new TaskOutcome("incomplete", "结束超时，字幕不完整", "cleanup");
        }
    }

    private async Task<TaskOutcome> RunAsync(TaskOptions options, Action<UiMessage> onMessage)
    {
        var info = new ProcessStartInfo(Path.Combine(options.Root, ".venv", "Scripts", "python.exe"))
        {
            WorkingDirectory = options.Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
            StandardInputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
        };
        var arguments = new List<string> { "-u", "-m", "subtitle_cli.ui_bridge", options.Mode,
                     options.Mode == "offline" ? "--input" : "--audio-device", options.Input,
                     "--source-lang", options.SourceLanguage, "--mt-model", options.Model,
                     "--mt-device", options.Device, "--mt-slots", options.Mode == "offline" ? "3" : "1",
                     "--output", options.Output };
        // Only sent when the user wrote something, so an empty box and no box behave the same.
        if (options.Hotwords.Length > 0) arguments.AddRange(new[] { "--hotwords", options.Hotwords });
        if (options.MtPrompt.Length > 0) arguments.AddRange(new[] { "--mt-prompt", options.MtPrompt });
        foreach (string part in arguments) info.ArgumentList.Add(part);
        info.Environment["PYTHONIOENCODING"] = "utf-8";

        using var process = new Process { StartInfo = info };
        using var job = new JobObject();
        bool started = false;
        try
        {
            if (!process.Start()) throw new IOException("Python 任务未能启动。");
            started = true;
            job.Add(process);
            lock (_gate) _process = process;
            UiMessage? finished = null;
            string? protocolError = null;
            int lastIndex = 0;
            long lastEnd = 0;

            async Task ReadOutputAsync()
            {
                while (await process.StandardOutput.ReadLineAsync() is { } line)
                {
                    try
                    {
                        var message = Parse(line);
                        if (finished is not null) throw new FormatException("结束消息后仍有输出。");
                        if (message.Type == "finished") finished = message;
                        else if (message.Type == "subtitle")
                        {
                            if (message.Index != lastIndex + 1 || message.StartMs < lastEnd || message.EndMs <= message.StartMs)
                                throw new FormatException("字幕序号或时间不连续。");
                            lastIndex = message.Index!.Value;
                            lastEnd = message.EndMs!.Value;
                        }
                        onMessage(message);
                    }
                    catch (Exception exc) when (exc is JsonException or FormatException or KeyNotFoundException or InvalidOperationException)
                    {
                        protocolError ??= exc.Message;
                    }
                }
            }

            async Task ReadErrorAsync()
            {
                while (await process.StandardError.ReadLineAsync() is { } line)
                {
                    lock (_gate)
                    {
                        _stderrTail.Enqueue(line.Length > 300 ? line[..300] : line);
                        while (_stderrTail.Count > 12) _stderrTail.Dequeue();
                    }
                }
            }

            var outputTask = ReadOutputAsync();
            var errorTask = ReadErrorAsync();
            await process.WaitForExitAsync();
            await Task.WhenAll(outputTask, errorTask);
            if (_timedOut) return new TaskOutcome("incomplete", "结束超时，字幕不完整", "cleanup");
            if (protocolError is not null) return new TaskOutcome("failed", $"通信错误：{protocolError}", "ui_bridge");
            if (finished is null) return new TaskOutcome("failed", $"任务未发送结束消息（退出码 {process.ExitCode}）。{ErrorTail()}", "ui_bridge");
            if (process.ExitCode != 0 && finished.Result != "failed")
                return new TaskOutcome("failed", $"任务异常退出（退出码 {process.ExitCode}）。{ErrorTail()}", "ui_bridge");
            if (finished.Result is not ("completed" or "stopped" or "incomplete" or "failed"))
                return new TaskOutcome("failed", "未知结束结果。", "ui_bridge");
            return new TaskOutcome(finished.Result!, finished.Message ?? "任务已结束", finished.Stage);
        }
        catch (Exception exc)
        {
            if (started && !process.HasExited) process.Kill(entireProcessTree: true);
            return new TaskOutcome("failed", $"任务进程错误：{exc.Message}", "process");
        }
        finally
        {
            lock (_gate) _process = null;
        }
    }

    private string ErrorTail()
    {
        lock (_gate) return string.Join(" | ", _stderrTail.TakeLast(2));
    }

    internal static UiMessage Parse(string line)
    {
        using var json = JsonDocument.Parse(line);
        var root = json.RootElement;
        string type = root.GetProperty("type").GetString() ?? throw new FormatException("缺少消息类型。");
        string? String(string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
        int? Int(string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt32() : null;
        long? Long(string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt64() : null;
        double? Double(string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetDouble() : null;
        return type switch
        {
            // A status carries either a new stage, or one item inside the model load. The
            // detail-only form leaves the stage label the UI already shows untouched, so a
            // slow start can report progress without inventing a new stage name.
            "status" when String("stage") is not null => new(type, Stage: String("stage"), CapturedSeconds: Int("captured_seconds"), Detail: String("detail"), Seconds: Double("seconds")),
            "status" when String("detail") is not null => new(type, Detail: String("detail"), Seconds: Double("seconds")),
            "status" => throw new FormatException("缺少阶段。"),
            // Offline progress: measured audio seconds against the length of the file, plus
            // the throughput so far. Nothing is derived from a guessed percentage, so the
            // bar only moves when the run has actually processed audio.
            "progress" => new(type, ProcessedSeconds: Double("processed_seconds") ?? throw new FormatException("缺少已处理时长。"), TotalSeconds: Double("total_seconds"), EtaSeconds: Double("eta_seconds"), Speed: Double("speed"), DecodedSeconds: Double("decoded_seconds"), SegmentsDone: Int("segments_done"), SegmentsOpen: Int("segments_open")),
            // Live mode before the first speech: how long the task has been listening.
            "listening" => new(type, WaitSeconds: Double("waited_seconds") ?? throw new FormatException("缺少等待时长。"), TimeoutSeconds: Double("timeout_seconds"), RemainingSeconds: Double("remaining_seconds")),
            "subtitle" => new(type, Index: Int("index") ?? throw new FormatException("缺少序号。"), StartMs: Long("start_ms") ?? throw new FormatException("缺少起点。"), EndMs: Long("end_ms") ?? throw new FormatException("缺少终点。"), Text: String("text") ?? throw new FormatException("缺少字幕。")),
            "finished" => new(type, Stage: String("stage"), Result: String("result") ?? throw new FormatException("缺少结果。"), Message: String("message") ?? throw new FormatException("缺少说明。")),
            _ => throw new FormatException("未知消息类型。"),
        };
    }
}
