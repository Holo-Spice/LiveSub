using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace LiveSub.Launcher;

/// <summary>
/// Exercises the floating subtitle window without a subtitle task: it shows the window with a
/// sample line, checks the window styles that make it click-through and always on top, and
/// renders the window's own visual tree to a PNG so what the user will see can be inspected.
///
/// This exists because UI automation cannot verify the overlay: it is a layered, click-through
/// window. The styles plus the rendered bitmap are the parts that can actually be checked
/// without a person looking at the screen.
/// </summary>
internal static class OverlaySelfCheck
{
    private const int GwlExstyle = -20;
    private const int WsExTransparent = 0x00000020;
    private const int WsExLayered = 0x00080000;
    private const int WsExNoActivate = 0x08000000;
    private const int WsExToolWindow = 0x00000080;

    public static void Run(string root, int seconds)
    {
        var lines = new List<string>();
        try
        {
            var overlay = new SubtitleOverlayWindow(root);
            overlay.Show("这是悬浮字幕的检查文本 · LiveSub overlay check");
            overlay.UpdateLayout();
            for (int i = 0; i < 40 && overlay.ActualWidth <= 0; i++)
            {
                overlay.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Render);
                Thread.Sleep(50);
            }
            // One render pass, so the caption has been laid out and painted.
            overlay.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.ContextIdle);

            var handle = new WindowInteropHelper(overlay).Handle;
            long style = GetWindowLongPtr(handle, GwlExstyle).ToInt64();
            lines.Add($"handle=0x{handle.ToInt64():X} visible={overlay.IsVisible} topmost={overlay.Topmost} show_in_taskbar={overlay.ShowInTaskbar}");
            lines.Add($"style=0x{style:X8} transparent={(style & WsExTransparent) != 0} layered={(style & WsExLayered) != 0} noactivate={(style & WsExNoActivate) != 0} toolwindow={(style & WsExToolWindow) != 0}");
            lines.Add($"caption=\"{overlay.Current}\"");
            lines.Add($"bounds=({overlay.Left:F0},{overlay.Top:F0}) {overlay.ActualWidth:F0}x{overlay.ActualHeight:F0}");
            lines.Add($"work_area={SystemParameters.WorkArea.Width:F0}x{SystemParameters.WorkArea.Height:F0}");

            foreach (var entry in Measure(overlay)) lines.Add(entry);
            lines.Add(Render(overlay, Path.Combine(root, "outputs", "overlay-check", "overlay.png")));
            overlay.Close();
        }
        catch (Exception exc)
        {
            lines.Add($"failed: {exc.GetType().Name}: {exc.Message}");
        }

        foreach (var line in lines) App.ReportToConsole("overlay-check: " + line);
        try
        {
            var folder = Path.Combine(root, "outputs", "overlay-check");
            Directory.CreateDirectory(folder);
            File.WriteAllLines(Path.Combine(folder, "overlay-check.txt"), lines, Encoding.UTF8);
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
        if (seconds > 0) Thread.Sleep(TimeSpan.FromSeconds(seconds));
    }

    /// <summary>The parts a user actually sees: the caption box and the drag bar.</summary>
    private static IEnumerable<string> Measure(Window overlay)
    {
        foreach (var name in new[] { "Caption", "DragBar" })
        {
            if (overlay.FindName(name) is FrameworkElement element)
                yield return $"{name}: actual={element.ActualWidth:F0}x{element.ActualHeight:F0} visible={element.IsVisible} text=\"{Text(element)}\"";
        }
    }

    private static string Text(FrameworkElement element) => element is System.Windows.Controls.TextBlock block ? block.Text : "";

    /// <summary>Renders the window's visual tree, which is what the layered window shows.</summary>
    private static string Render(Window overlay, string path)
    {
        var width = (int)Math.Ceiling(overlay.ActualWidth);
        var height = (int)Math.Ceiling(overlay.ActualHeight);
        if (width <= 0 || height <= 0) return "render skipped: empty size";
        var target = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
        target.Render(overlay);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(target));
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using (var stream = File.Create(path)) encoder.Save(stream);

        // The caption is white on a dark strip, so a rendered caption means bright pixels in
        // the lower part of the image and none near the very top edge.
        var pixels = new byte[width * height * 4];
        target.CopyPixels(pixels, width * 4, 0);
        int bright = 0, dark = 0, opaque = 0;
        for (int index = 0; index < pixels.Length; index += 4)
        {
            int luma = (pixels[index] * 114 + pixels[index + 1] * 587 + pixels[index + 2] * 299) / 1000;
            if (pixels[index + 3] > 8) opaque++;
            if (luma > 150) bright++;
            if (luma < 60 && pixels[index + 3] > 8) dark++;
        }
        var total = width * height;
        return $"render={path} bright_ratio={bright * 1.0 / total:F4} dark_ratio={dark * 1.0 / total:F4} opaque_ratio={opaque * 1.0 / total:F4}";
    }

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr(IntPtr handle, int index);
}
