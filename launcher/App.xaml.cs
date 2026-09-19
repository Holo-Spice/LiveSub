using System.Configuration;
using System.Data;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;

namespace LiveSub.Launcher;

/// <summary>
/// Interaction logic for App.xaml
/// </summary>
public partial class App : Application
{
    private Mutex? _instance;

    [DllImport("kernel32.dll")]
    private static extern bool AttachConsole(int processId);

    protected override void OnStartup(StartupEventArgs e)
    {
        // A self check for the floating subtitle window: it verifies the click-through window
        // styles and what is really drawn on screen, which is the only way to test an overlay
        // that is invisible to UI automation. It never starts a subtitle task.
        if (e.Args.Length == 3 && e.Args[0] == "--overlay-check")
        {
            base.OnStartup(e);
            OverlaySelfCheck.Run(e.Args[1], int.Parse(e.Args[2]));
            Shutdown();
            return;
        }
        _instance = new Mutex(true, @"Local\LiveSub.SingleInstance", out bool first);
        if (!first)
        {
            MessageBox.Show("LiveSub 已在运行。", "LiveSub", MessageBoxButton.OK, MessageBoxImage.Information);
            Shutdown();
            return;
        }
        base.OnStartup(e);
        string? existingRoot = null;
        if (e.Args.Length > 0)
        {
            if (e.Args.Length != 2 || e.Args[0] != "--existing-root")
            {
                MessageBox.Show("用法：LiveSub.Launcher.exe --existing-root <已配置的 LiveSub 目录>", "LiveSub", MessageBoxButton.OK, MessageBoxImage.Error);
                Shutdown();
                return;
            }
            try
            {
                existingRoot = Path.GetFullPath(e.Args[1]);
                if (!File.Exists(Path.Combine(existingRoot, "config.toml")) ||
                    !File.Exists(Path.Combine(existingRoot, ".venv", "Scripts", "python.exe")))
                    throw new IOException("目录缺少 config.toml 或 .venv\\Scripts\\python.exe。");
            }
            catch (Exception exc) when (exc is IOException or ArgumentException or NotSupportedException)
            {
                MessageBox.Show("无法使用现有环境：" + exc.Message, "LiveSub", MessageBoxButton.OK, MessageBoxImage.Error);
                Shutdown();
                return;
            }
        }
        else
        {
            // Started by double clicking the icon or a shortcut: if the program is sitting in a
            // directory that already holds a complete environment, that is the one to use, so no
            // command line and no arguments are needed at all.
            foreach (string candidate in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
            {
                try
                {
                    var root = Path.GetFullPath(candidate);
                    if (File.Exists(Path.Combine(root, "config.toml")) && File.Exists(Path.Combine(root, ".venv", "Scripts", "python.exe")))
                    {
                        existingRoot = root;
                        break;
                    }
                }
                catch (Exception exc) when (exc is ArgumentException or NotSupportedException or PathTooLongException) { }
            }
        }
        new MainWindow(existingRoot).Show();
    }

    /// <summary>Writes one line to the console of the parent process, when there is one.</summary>
    internal static void ReportToConsole(string line)
    {
        if (!AttachConsole(-1)) return;
        var stream = new FileStream("CONOUT$", FileMode.Open, FileAccess.Write);
        using var writer = new StreamWriter(stream, Encoding.UTF8) { AutoFlush = true };
        writer.WriteLine(line);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        if (_instance is not null)
        {
            _instance.ReleaseMutex();
            _instance.Dispose();
        }
        base.OnExit(e);
    }
}

