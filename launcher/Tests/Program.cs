using System.IO;
using System.IO.Compression;
using System.Formats.Tar;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using LiveSub.Launcher;

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

var project = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", ".."));
var launcher = Path.Combine(project, "launcher");
var manifest = InstallManifest.Load(launcher);
Check(manifest.Components.Count >= 20 && manifest.Components.Count(c => c.Optional) == 1, "固定清单内容");
Check(manifest.Components.Single(c => c.Id == "rocm-runtime").Sources.Count == 2, "ROCm 双归档");
Check(manifest.Components.Single(c => c.Id == "hymt-7b").Optional == false, "7B 必装");
Console.WriteLine("清单与空间：通过");

var temp = Path.Combine(Path.GetTempPath(), "LiveSub 快速检查 " + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temp);
try
{
    var target = InstallManifest.UnderRoot(temp, "中文 空格/video.mkv");
    Check(target.StartsWith(temp, StringComparison.OrdinalIgnoreCase), "中文路径");
    try { InstallManifest.UnderRoot(temp, "../escape"); throw new Exception("越界路径未拒绝"); }
    catch (InvalidDataException) { }
    Console.WriteLine("参数与路径：通过");

    var component = new InstallComponent
    {
        Id = "tiny", Name = "小文件", Target = "files/tiny.bin", RequiredPaths = ["files/tiny.bin"],
        InstalledSizeBytes = 3, Sources = [new DownloadSource { Url = "https://example.invalid/tiny", DownloadSizeBytes = 3, Sha256 = new string('A', 64), Archive = "none" }],
    };
    var state = new InstallState();
    await state.SaveAsync(temp, CancellationToken.None);
    await state.ClaimAsync(temp, component.Id, component.Target, CancellationToken.None);
    Directory.CreateDirectory(Path.GetDirectoryName(InstallManifest.UnderRoot(temp, component.Target))!);
    await File.WriteAllBytesAsync(InstallManifest.UnderRoot(temp, component.Target), [1, 2, 3]);
    Check(!state.IsComplete(temp, component), "未完成组件不可假报成功");
    await state.CompleteAsync(temp, component, CancellationToken.None);
    Check(InstallState.Load(temp)!.IsComplete(temp, component), "完成状态应保存");
    await File.WriteAllBytesAsync(InstallManifest.UnderRoot(temp, component.Target), [1, 2]);
    Check(!InstallState.Load(temp)!.IsComplete(temp, component), "关键文件变更应失效");
    var unknown = Path.Combine(temp, "unknown.bin");
    await File.WriteAllBytesAsync(unknown, [1]);
    try { await state.ClaimAsync(temp, "other", "unknown.bin", CancellationToken.None); throw new Exception("未知文件未拒绝"); }
    catch (IOException) { }
    Console.WriteLine("安装恢复：通过");

    var zipFile = Path.Combine(temp, "small.zip");
    using (var zip = ZipFile.Open(zipFile, ZipArchiveMode.Create))
    {
        var good = zip.CreateEntry("folder/ok.txt");
        await using (var output = good.Open()) await output.WriteAsync(Encoding.UTF8.GetBytes("ok"));
    }
    var staging = Path.Combine(temp, "staging");
    Directory.CreateDirectory(staging);
    Installer.ExtractZip(zipFile, staging, 1, CancellationToken.None);
    Check(File.ReadAllText(Path.Combine(staging, "ok.txt")) == "ok", "小归档解压");
    var badZip = Path.Combine(temp, "bad.zip");
    using (var zip = ZipFile.Open(badZip, ZipArchiveMode.Create)) zip.CreateEntry("../escape.txt");
    try { Installer.ExtractZip(badZip, staging, 0, CancellationToken.None); throw new Exception("越界归档未拒绝"); }
    catch (InvalidDataException) { }
    await using (var server = new SmallServer(Encoding.UTF8.GetBytes("small file payload")))
    {
        var bytes = Encoding.UTF8.GetBytes("small file payload");
        var sha = Convert.ToHexString(SHA256.HashData(bytes));
        var download = new DownloadService();
        var good = new DownloadSource { Url = server.Url("normal"), DownloadSizeBytes = bytes.Length, Sha256 = sha, Archive = "none" };
        var part = Path.Combine(temp, "download.part");
        await download.DownloadAsync(good, part, null, CancellationToken.None);
        Check(File.ReadAllBytes(part).SequenceEqual(bytes), "正常下载");
        await File.WriteAllBytesAsync(part, bytes[..5]);
        await download.DownloadAsync(good, part, null, CancellationToken.None);
        Check(server.RangeRequests > 0 && File.ReadAllBytes(part).SequenceEqual(bytes), "206 续传");
        await File.WriteAllBytesAsync(part, bytes[..5]);
        await download.DownloadAsync(good with { Url = server.Url("no-range") }, part, null, CancellationToken.None);
        Check(File.ReadAllBytes(part).SequenceEqual(bytes), "200 从零重下");
        await File.WriteAllBytesAsync(part, bytes[..5]);
        await download.DownloadAsync(good with { Url = server.Url("force-416") }, part, null, CancellationToken.None);
        Check(File.ReadAllBytes(part).SequenceEqual(bytes), "416 恢复");
        try { await download.DownloadAsync(good with { Sha256 = new string('0', 64) }, part, null, CancellationToken.None); throw new Exception("坏哈希未拒绝"); }
        catch (InvalidDataException) { }
        Check(!File.Exists(part), "坏哈希应清理当前临时文件");

        var installed = new InstallComponent
        {
            Id = "tiny-http", Name = "小组件", Target = "files/downloaded.bin", RequiredPaths = ["files/downloaded.bin"],
            InstalledSizeBytes = bytes.Length, Sources = [good],
        };
        var installState = InstallState.Load(temp)!;
        var installer = new Installer();
        await installer.InstallComponentAsync(temp, installed, installState, new Progress<SetupProgress>(_ => { }), CancellationToken.None);
        Check(installState.IsComplete(temp, installed) && File.ReadAllBytes(InstallManifest.UnderRoot(temp, installed.Target)).SequenceEqual(bytes), "下载后提交并保存状态");
        int requests = server.RequestCount;
        await installer.InstallComponentAsync(temp, installed, installState, new Progress<SetupProgress>(_ => { }), CancellationToken.None);
        Check(server.RequestCount == requests, "重开跳过已完成组件");

        static byte[] TinyTar(string contents)
        {
            using var memory = new MemoryStream();
            using (var gzip = new GZipStream(memory, CompressionLevel.SmallestSize, leaveOpen: true))
            using (var tar = new TarWriter(gzip, leaveOpen: true))
            {
                var entry = new PaxTarEntry(TarEntryType.RegularFile, "./lib/a.txt") { DataStream = new MemoryStream(Encoding.UTF8.GetBytes(contents)) };
                tar.WriteEntry(entry);
            }
            return memory.ToArray();
        }
        var archiveOne = TinyTar("first");
        var archiveTwo = TinyTar("second");
        server.SetRoute("archive-one", archiveOne);
        server.SetRoute("archive-two", archiveTwo);
        var overlay = new InstallComponent
        {
            Id = "rocm-runtime", Name = "双归档", Target = "tools/rocm-test", RequiredPaths = ["tools/rocm-test/lib/a.txt"],
            InstalledSizeBytes = Encoding.UTF8.GetByteCount("second"),
            Sources = [
                new DownloadSource { Url = server.Url("archive-one"), DownloadSizeBytes = archiveOne.Length, Sha256 = Convert.ToHexString(SHA256.HashData(archiveOne)), Archive = "tar.gz", StripComponents = 1 },
                new DownloadSource { Url = server.Url("archive-two"), DownloadSizeBytes = archiveTwo.Length, Sha256 = Convert.ToHexString(SHA256.HashData(archiveTwo)), Archive = "tar.gz", StripComponents = 1 },
            ],
        };
        await installer.InstallComponentAsync(temp, overlay, installState, new Progress<SetupProgress>(_ => { }), CancellationToken.None);
        Check(installState.IsComplete(temp, overlay) && File.ReadAllText(InstallManifest.UnderRoot(temp, overlay.RequiredPaths[0])) == "second", "双归档顺序合并");
    }
    Console.WriteLine("下载与解压边界：通过");

    var status = TaskRunner.Parse("{\"type\":\"status\",\"stage\":\"asr\"}");
    var subtitle = TaskRunner.Parse("{\"type\":\"subtitle\",\"index\":1,\"start_ms\":0,\"end_ms\":1000,\"text\":\"你好\"}");
    var finished = TaskRunner.Parse("{\"type\":\"finished\",\"result\":\"completed\",\"message\":\"完成\"}");
    Check(status.Stage == "asr" && subtitle.Text == "你好" && finished.Result == "completed", "消息字段");
    var loadStep = TaskRunner.Parse("{\"type\":\"status\",\"detail\":\"asr_weights\",\"seconds\":null}");
    var loadDone = TaskRunner.Parse("{\"type\":\"status\",\"detail\":\"asr_weights\",\"seconds\":1.14}");
    Check(loadStep.Stage is null && loadStep.Detail == "asr_weights" && loadStep.Seconds is null, "模型加载步骤开始");
    Check(loadDone.Detail == "asr_weights" && loadDone.Seconds == 1.14, "模型加载步骤耗时");
    var captured = TaskRunner.Parse("{\"type\":\"status\",\"stage\":\"capturing\",\"captured_seconds\":7}");
    Check(captured.CapturedSeconds == 7, "采集秒数");
    // Offline progress and the live listening wait are measurements reported by the CLI, so
    // the UI can show a real percentage and a remaining time instead of guessing either.
    var progress = TaskRunner.Parse("{\"type\":\"progress\",\"total_seconds\":600.0,\"decoded_seconds\":320.5,\"processed_seconds\":281.2,\"eta_seconds\":142.0,\"speed\":4.02,\"segments_done\":41,\"segments_open\":2}");
    Check(progress.ProcessedSeconds == 281.2 && progress.TotalSeconds == 600.0 && progress.EtaSeconds == 142.0, "离线进度字段");
    Check(progress.Speed == 4.02 && progress.DecodedSeconds == 320.5 && progress.SegmentsDone == 41 && progress.SegmentsOpen == 2, "离线进度明细");
    var noEta = TaskRunner.Parse("{\"type\":\"progress\",\"processed_seconds\":12.0}");
    Check(noEta.ProcessedSeconds == 12.0 && noEta.TotalSeconds is null && noEta.EtaSeconds is null, "无总时长时仍接受进度");
    try { TaskRunner.Parse("{\"type\":\"progress\",\"total_seconds\":10.0}"); throw new Exception("无已处理时长的进度未拒绝"); }
    catch (FormatException) { }
    var listening = TaskRunner.Parse("{\"type\":\"listening\",\"waited_seconds\":15.0,\"timeout_seconds\":300.0,\"remaining_seconds\":285.0}");
    Check(listening.WaitSeconds == 15.0 && listening.TimeoutSeconds == 300.0 && listening.RemainingSeconds == 285.0, "等待语音字段");
    try { TaskRunner.Parse("{\"type\":\"listening\",\"timeout_seconds\":300.0}"); throw new Exception("无等待时长的等待消息未拒绝"); }
    catch (FormatException) { }
    try { TaskRunner.Parse("{\"type\":\"status\"}"); throw new Exception("无阶段无步骤的状态未拒绝"); }
    catch (FormatException) { }
    try { TaskRunner.Parse("{\"type\":\"subtitle\","); throw new Exception("半行消息未拒绝"); }
    catch (System.Text.Json.JsonException) { }
    Console.WriteLine("私有消息：通过");

    // The overlay timing, reproduced with the batch a measured live run actually produced: one
    // utterance of 19.5s..32.9s written as five lines at the same instant. The overlay must
    // hand them out over the time they were spoken instead of drawing all five at once.
    var schedule = new OverlaySchedule();
    (string, long, long, bool)[] utterance =
    [
        ("虽然相识的方式已经彻底变得现代化，用上了最新的应用。", 19522, 23602, false),
        ("对，就是之后如何建立关系的问题吧。", 23682, 26322, false),
        ("是的。", 26322, 27042, false),
        ("之后维持关系的方式，还保留着极其古老传统的规矩呢。", 27282, 32882, false),
        ("没错。", 31502, 32382, true),
    ];
    // The batch is handed over as the utterance finishes, which is what the live run does: the
    // audio it describes is already fully spoken, so no line is held back for a start that has
    // passed. Every line still gets shown, in order and for its own reading time.
    schedule.Push(utterance, 1, elapsedMs: 13932);
    var seenLines = new List<string>();
    for (long tick = 1; tick <= 500; tick++)
    {
        schedule.Advance(tick);
        if (schedule.Visible.Length > 0 && (seenLines.Count == 0 || seenLines[^1] != schedule.Visible)) seenLines.Add(schedule.Visible);
    }
    Check(seenLines.Count == 5, $"整段五句应依次显示，实际 {seenLines.Count} 句（明细=[{string.Join(" | ", seenLines)}]）");
    Check(schedule.Shown == 5, $"整段五句应有 5 次交付，实际 {schedule.Shown}");
    Check(seenLines[0].StartsWith("虽然相识") && seenLines[2] == "是的。", $"顺序应为原句顺序，实际 {string.Join(" / ", seenLines)}");
    Check(seenLines[^1] == "没错。", $"最后停在整段最后一句，实际 {seenLines[^1]}");

    // The long line stays readable while the short line after it is already waiting: this is the
    // complaint that started the fix, so it is checked on its own. The batch landed 2.3s after
    // the utterance began, so the first line is due 2.3s from now and the long line gets its
    // whole reading time instead of being wiped by the short phrase that follows it.
    var pacing = new OverlaySchedule();
    pacing.Push(utterance, 1, elapsedMs: 13932 - 2300);
    bool firstAdvance = pacing.Advance(77);
    Check(firstAdvance == false && pacing.Visible.Length == 0, $"音频还没到第一句时不得提前出现（可见={pacing.Visible.Length}）");
    pacing.Advance(79);
    Check(pacing.Visible.StartsWith("虽然相识"), $"第一句的音频到了就应出现，实际 {pacing.Visible}");
    for (long tick = 80; tick <= 119; tick++) pacing.Advance(tick);
    Check(pacing.Visible.StartsWith("虽然相识"), "长句自身时长未走完前不得被顶掉");
    pacing.Advance(120);
    Check(pacing.Visible.StartsWith("虽然相识"), "长句自身时长未走完前不得被顶掉");
    pacing.Advance(121);
    Check(pacing.Visible.StartsWith("对，就是"), $"长句读完后应切换到第二句，实际 {pacing.Visible}");
    for (long tick = 122; tick <= 148; tick++) pacing.Advance(tick);
    Check(pacing.Visible.StartsWith("对，就是"), "第二句应按自身时长停留");
    pacing.Advance(149);
    Check(pacing.Visible.StartsWith("是的"), $"第二句时长用完后应切换，实际 {pacing.Visible}");

    // The last line of an utterance is left on screen through the pause, then cleared.
    var linger = new OverlaySchedule();
    linger.Push([("这是整段的最后一句。", 0, 3000, true)], 1);
    linger.Advance(2);
    Check(linger.Visible.StartsWith("这是整段"), "最后一句应立即显示");
    Check(linger.Busy(100), "宽限期内仍算忙碌，不得显示正在等待语音");
    for (long tick = 3; tick <= 180; tick++) linger.Advance(tick);
    Check(linger.Visible.StartsWith("这是整段"), "宽限期内最后一句应留在屏幕上");
    Check(linger.Advance(184), $"宽限期结束时 Advance 应报告清空（可见={linger.Visible.Length}）");
    Check(linger.Visible.Length == 0, "宽限期结束后字幕应清空");
    Check(!linger.Busy(190), "清空后应回到等待语音状态");

    // A three-word line must not be wiped by the long line arriving 160ms later with it.
    var brief = new OverlaySchedule();
    brief.Push([("可是。", 15938, 16338, false), ("从现在开始才是今天最有趣的部分。", 16642, 19042, true)], 1, elapsedMs: 16538);
    bool briefFirst = brief.Advance(2);
    Check(briefFirst == false && brief.Visible == "可是。", $"短句立即出现（清除={briefFirst} 可见='{brief.Visible}'）");
    for (long tick = 3; tick <= 13; tick++) brief.Advance(tick);
    Check(brief.Visible == "可是。", "最短停留内的 0.16s 后继不得覆盖当前字幕");
    brief.Advance(14);
    Check(brief.Visible.StartsWith("从现在开始"), $"最短停留结束后必须切换，实际 {brief.Visible}");

    // A batch whose first lines are already in the past: they are drawn in order, at once, and
    // the batch still ends on the line the speaker has just reached.
    var late = new OverlaySchedule();
    late.Push([
        ("屏幕上早就过去的第一句。", 0, 1000, false),
        ("同样已经过去的一句。", 1500, 2500, false),
        ("这一句才刚刚说到。", 3000, 4500, true),
    ], 1, elapsedMs: 4000);
    Check(late.Advance(2) == false && late.Visible.StartsWith("屏幕上早"), $"过期批次应按原顺序补上，实际 {late.Visible}");
    for (long tick = 3; tick <= 13; tick++) late.Advance(tick);
    Check(late.Advance(14) == false && late.Visible.StartsWith("同样已经过去"), $"两句过期后应切到第二句，实际 {late.Visible}");
    for (long tick = 15; tick <= 41; tick++) late.Advance(tick);
    Check(late.Advance(42) == false && late.Visible.StartsWith("这一句"), $"最后应停在刚说到的一句，实际 {late.Visible}");
    Check(late.Shown == 3, $"交付计数应为本批三句，实际 {late.Shown}");
    // A line whose audio is still ahead waits for it instead of appearing early.
    var ahead = new OverlaySchedule();
    ahead.Push([("还没说到的第一句。", 0, 2000, false), ("更后面的第二句。", 4000, 6000, true)], 1, elapsedMs: 1000);
    Check(ahead.Advance(2) == false && ahead.Visible.StartsWith("还没说到"), "已经过去的一句应立即出现");
    Check(ahead.Advance(30) == false && ahead.Visible.StartsWith("还没说到"), "后续句子必须等它自己的音频");
    Check(ahead.Advance(31) == false && ahead.Visible.StartsWith("更后面"), $"到点后应切换到第二句，实际 {ahead.Visible}");
    Console.WriteLine("悬浮字幕排期：通过");

    var fake = Path.Combine(temp, "进程 根");
    Directory.CreateDirectory(Path.Combine(fake, ".venv", "Scripts"));
    Directory.CreateDirectory(Path.Combine(fake, "subtitle_cli"));
    var externalPython = Environment.GetEnvironmentVariable("LIVESUB_TEST_PYTHON");
    if (string.IsNullOrWhiteSpace(externalPython))
    {
        var sourceVenv = Path.Combine(project, ".venv");
        File.Copy(Path.Combine(sourceVenv, "Scripts", "python.exe"), Path.Combine(fake, ".venv", "Scripts", "python.exe"));
        File.Copy(Path.Combine(sourceVenv, "pyvenv.cfg"), Path.Combine(fake, ".venv", "pyvenv.cfg"));
    }
    else
    {
        File.Copy(externalPython, Path.Combine(fake, ".venv", "Scripts", "python.exe"));
        await File.WriteAllTextAsync(Path.Combine(fake, ".venv", "pyvenv.cfg"),
            $"home = {Path.GetDirectoryName(externalPython)}{Environment.NewLine}include-system-site-packages = false{Environment.NewLine}");
    }
    await File.WriteAllTextAsync(Path.Combine(fake, "subtitle_cli", "__init__.py"), "");
    await File.WriteAllTextAsync(Path.Combine(fake, "subtitle_cli", "ui_bridge.py"), """
import json, sys, time
args=sys.argv[1:]
mode=args[0]
source=args[args.index('--input')+1] if mode=='offline' else args[args.index('--audio-device')+1]
if source=='abnormal': sys.exit(2)
print(json.dumps({'type':'status','stage':'vad'}),flush=True)
if mode=='offline':
 assert source.endswith('中文 空格.mkv')
 print(json.dumps({'type':'subtitle','index':1,'start_ms':0,'end_ms':1000,'text':'你好'},ensure_ascii=False),flush=True)
 print(json.dumps({'type':'finished','result':'completed','message':'完成'},ensure_ascii=False),flush=True)
elif source=='ignore':
 while True: time.sleep(1)
else:
 for line in sys.stdin:
  if json.loads(line).get('command')=='stop':
   print(json.dumps({'type':'finished','result':'stopped','message':'已停止'},ensure_ascii=False),flush=True)
   break
""", Encoding.UTF8);
    var seen = new List<UiMessage>();
    var runner = new TaskRunner(TimeSpan.FromMilliseconds(350));
    var normal = await runner.Start(new TaskOptions(fake, "offline", Path.Combine(fake, "中文 空格.mkv"), "auto", "7b", "gpu", Path.Combine(fake, "out.srt")), seen.Add);
    Check(normal.Result == "completed" && seen.Count(m => m.Type == "subtitle") == 1, "正常完成与完整字幕");
    var abnormal = await runner.Start(new TaskOptions(fake, "offline", "abnormal", "auto", "7b", "gpu", Path.Combine(fake, "out2.srt")), _ => { });
    Check(abnormal.Result == "failed", "异常退出不可显示完成");
    var liveTask = runner.Start(new TaskOptions(fake, "live", "loopback", "auto", "7b", "gpu", Path.Combine(fake, "live.srt")), _ => { });
    var stopped = await runner.StopAsync();
    Check(stopped?.Result == "stopped" && stopped.Message == "已停止", "stdin 停止与 UTF-8 消息");
    var timeoutTask = runner.Start(new TaskOptions(fake, "live", "ignore", "auto", "7b", "gpu", Path.Combine(fake, "timeout.srt")), _ => { });
    var timeout = await runner.StopAsync();
    Check(timeout?.Result == "incomplete", "超时回收");
    Console.WriteLine("启停与释放：通过");
}
finally
{
    var tmp = Path.GetFullPath(Path.GetTempPath());
    if (!Path.GetFullPath(temp).StartsWith(tmp, StringComparison.OrdinalIgnoreCase)) throw new Exception("测试目录越界");
    Directory.Delete(temp, recursive: true);
}

Console.WriteLine("快速检查完成");

sealed class SmallServer : IAsyncDisposable
{
    private readonly byte[] _content;
    private readonly Dictionary<string, byte[]> _routes = new();
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _stop = new();
    private readonly Task _worker;
    public int RangeRequests { get; private set; }
    public int RequestCount { get; private set; }

    public SmallServer(byte[] content)
    {
        _content = content;
        _listener.Start();
        _worker = ServeAsync();
    }

    public string Url(string name) => $"http://127.0.0.1:{((IPEndPoint)_listener.LocalEndpoint).Port}/{name}";
    public void SetRoute(string name, byte[] content) => _routes[name] = content;

    private async Task ServeAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await _listener.AcceptTcpClientAsync(_stop.Token); }
            catch (OperationCanceledException) { break; }
            using (client) await ReplyAsync(client);
        }
    }

    private async Task ReplyAsync(TcpClient client)
    {
        var stream = client.GetStream();
        using var reader = new StreamReader(stream, Encoding.ASCII, leaveOpen: true);
        var first = await reader.ReadLineAsync() ?? "";
        RequestCount++;
        var route = first.Split(' ').ElementAtOrDefault(1) ?? "";
        var content = _routes.GetValueOrDefault(route.TrimStart('/'), _content);
        long? start = null;
        string? line;
        while (!string.IsNullOrEmpty(line = await reader.ReadLineAsync()))
        {
            if (line.StartsWith("Range: bytes=", StringComparison.OrdinalIgnoreCase) && long.TryParse(line[13..].TrimEnd('-'), out var parsed))
            {
                start = parsed;
                RangeRequests++;
            }
        }
        if (route == "/no-range") start = null;
        bool invalidRange = start is not null && (route == "/force-416" || start >= content.Length);
        int code = invalidRange ? 416 : start is null ? 200 : 206;
        var body = invalidRange ? [] : content[(int)(start ?? 0)..];
        var header = $"HTTP/1.1 {code} {(code == 200 ? "OK" : code == 206 ? "Partial Content" : "Range Not Satisfiable")}\r\nContent-Length: {body.Length}\r\nConnection: close\r\n";
        if (code == 206) header += $"Content-Range: bytes {start}-{content.Length - 1}/{content.Length}\r\n";
        if (code == 416) header += $"Content-Range: bytes */{content.Length}\r\n";
        header += "\r\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(header));
        await stream.WriteAsync(body);
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        _listener.Stop();
        try { await _worker; } catch (SocketException) { }
        _stop.Dispose();
    }
}
