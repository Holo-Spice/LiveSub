using System.IO;

namespace LiveSub.Launcher;

internal sealed class ProbeRunner(Func<string, IEnumerable<string>, string, CancellationToken, Task> run)
{
    public async Task<bool> RunAsync(string root, bool optionalModel, Action<string, bool, string> report, CancellationToken cancellationToken)
    {
        var python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        var checks = new List<(string Name, string[] Args)>
        {
            ("Python 3.12 / pip check", ["-m", "pip", "check"]),
            ("PyTorch HIP / FP16", ["-c", "import torch; assert torch.version.hip and torch.cuda.is_available(); x=torch.ones((16,16),device='cuda:0',dtype=torch.float16); assert float((x@x).mean())==16"]),
            ("FFmpeg / DirectShow / llama AMD GPU", ["-c", "from pathlib import Path; import subprocess; from subtitle_cli.cli import configured_paths,preflight; p=configured_paths(Path.cwd(),'7b'); preflight(p,'gpu'); d=subprocess.run([str(p['ffmpeg']),'-hide_banner','-devices'],capture_output=True,text=True,check=True); assert 'dshow' in d.stdout+d.stderr"]),
            ("Silero / Smart Turn / SaT", ["-c", "from pathlib import Path; import onnxruntime as ort; from silero_vad import load_silero_vad; from wtpsplit import SaT; from subtitle_cli.cli import configured_paths; p=configured_paths(Path.cwd(),'7b'); load_silero_vad(onnx=True); ort.InferenceSession(str(p['smart_turn']),providers=['CPUExecutionProvider']); SaT(str(p['sat']),tokenizer_name_or_path=str(p['sat_tokenizer']),ort_providers=['CPUExecutionProvider'])"]),
            ("CLI 帮助与实际配置", ["-c", "from pathlib import Path; import subprocess,sys; from subtitle_cli.cli import configured_paths; configured_paths(Path.cwd(),'7b'); subprocess.run([sys.executable,'-m','subtitle_cli.cli','--help'],check=True,stdout=subprocess.DEVNULL)"]),
        };
        foreach (var (name, args) in checks)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try { await run(python, args, root, cancellationToken); report(name, true, "通过"); }
            catch (Exception exc) when (exc is IOException or InvalidOperationException)
            { report(name, false, exc.Message); return false; }
        }
        var sample = Path.Combine(root, "testdata", "probe-en.mkv");
        if (!File.Exists(sample)) { report("7B GPU 离线短样本", false, "核心包缺少合法短样本"); return false; }
        var output = Path.Combine(root, ".setup", "probe", "probe-" + Guid.NewGuid().ToString("N") + ".srt");
        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        try
        {
            await run(python, ["-m", "subtitle_cli.cli", "offline", "--input", sample, "--source-lang", "en", "--mt-model", "7b", "--mt-device", "gpu", "--output", output], root, cancellationToken);
            if (!File.Exists(output) || new FileInfo(output).Length == 0) throw new IOException("短样本未产生字幕。");
            report("7B GPU 离线短样本", true, "通过");
        }
        catch (Exception exc) when (exc is IOException or InvalidOperationException)
        { report("7B GPU 离线短样本", false, exc.Message); return false; }

        if (optionalModel)
        {
            try
            {
                await run(python, ["-c", "from pathlib import Path\nfrom subtitle_cli.cli import configured_paths\nfrom subtitle_cli.translator import Translator\np=configured_paths(Path.cwd(),'1.8b')\nt=Translator(p['llama_server'],p['hymt_1_8b'],p['llama_runtime_bin'],'gpu')\ntry:\n print(t.translate('Hello.'))\nfinally:\n t.close()"], root, cancellationToken);
                report("1.8B 短句翻译", true, "通过");
            }
            catch (Exception exc) when (exc is IOException or InvalidOperationException)
            { report("1.8B 短句翻译", false, exc.Message); return false; }
        }
        else report("1.8B 短句翻译", true, "未安装，已跳过");
        return true;
    }
}
