using System.Globalization;
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
/// sample line, checks the window styles, verifies that only the drag bar answers the mouse, and
/// then drags the window the way a user would - with real mouse input - to prove the drag works.
/// Finally it renders the window's own visual tree to a PNG so what the user will see can be
/// inspected.
///
/// This exists because UI automation cannot verify the overlay: it is a layered window. The
/// styles, the hit-test answers and the position after a real drag are the parts that can
/// actually be checked without a person looking at the screen. The drag part moves the real
/// cursor, so it runs only under --overlay-check.
/// </summary>
internal static class OverlaySelfCheck
{
    private const int GwlExstyle = -20;
    private const int WsExTransparent = 0x00000020;
    private const int WsExLayered = 0x00080000;
    private const int WsExNoActivate = 0x08000000;
    private const int WsExToolWindow = 0x00000080;

    private const int WmNchittest = 0x0084;
    private const int HtCaption = 2;
    private const int SmtoAbortIfHung = 0x0002;

    private const uint MouseeventfMove = 0x0001;
    private const uint MouseeventfLeftdown = 0x0002;
    private const uint MouseeventfLeftup = 0x0004;
    private const uint MouseeventfAbsolute = 0x8000;

    public static void Run(string root, int seconds)
    {
        var lines = new List<string>();
        try
        {
            var overlay = new SubtitleOverlayWindow(root);
            overlay.ShowHint("这是悬浮字幕的检查文本 · LiveSub overlay check");
            overlay.UpdateLayout();
            for (int i = 0; i < 40 && overlay.ActualWidth <= 0; i++)
            {
                overlay.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Render);
                Thread.Sleep(50);
            }
            // Let the overlay's own 10 Hz timer hand the line to the caption before measuring.
            Pump(overlay, TimeSpan.FromMilliseconds(400));

            var handle = new WindowInteropHelper(overlay).Handle;
            long style = GetWindowLongPtr(handle, GwlExstyle).ToInt64();
            lines.Add($"handle=0x{handle.ToInt64():X} visible={overlay.IsVisible} topmost={overlay.Topmost} show_in_taskbar={overlay.ShowInTaskbar}");
            lines.Add($"style=0x{style:X8} transparent={(style & WsExTransparent) != 0} layered={(style & WsExLayered) != 0} noactivate={(style & WsExNoActivate) != 0} toolwindow={(style & WsExToolWindow) != 0}");
            if ((style & WsExTransparent) != 0)
                lines.Add("failed: WS_EX_TRANSPARENT is still set, the drag bar cannot receive the mouse");
            lines.Add($"caption=\"{overlay.Current}\"");
            var area = SystemParameters.WorkArea;
            lines.Add($"bounds=({overlay.Left:F0},{overlay.Top:F0}) {overlay.ActualWidth:F0}x{overlay.ActualHeight:F0}");
            lines.Add($"work_area={area.Width:F0}x{area.Height:F0}");
            if (overlay.Top + overlay.ActualHeight > area.Bottom + 1)
                lines.Add("failed: the window hangs below the work area, its drag bar could be unreachable");

            foreach (var entry in Measure(overlay)) lines.Add(entry);
            lines.AddRange(HitTest(handle, overlay));
            lines.AddRange(Drag(handle, overlay, root));
            lines.AddRange(Opacity(overlay, root));
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

    private static void Pump(Window window, TimeSpan duration)
    {
        var until = DateTime.UtcNow + duration;
        while (DateTime.UtcNow < until)
        {
            window.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
            Thread.Sleep(20);
        }
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

    /// <summary>
    /// What the window answers for a point on the bar and a point on the caption. The bar must
    /// report as a caption (so the window manager drags it) and the caption as transparent (so
    /// the click belongs to the application below).
    /// </summary>
    private static IEnumerable<string> HitTest(IntPtr handle, Window overlay)
    {
        var bar = PointToScreen(overlay, overlay.ActualWidth / 2, 8);
        var caption = PointToScreen(overlay, overlay.ActualWidth / 2, overlay.ActualHeight - 8);
        int barCode = Hit(handle, bar);
        int captionCode = Hit(handle, caption);
        yield return $"hittest bar=({bar.X},{bar.Y}) -> {barCode}{(barCode == HtCaption ? " (caption)" : " (unexpected)")}";
        yield return $"hittest caption=({caption.X},{caption.Y}) -> {captionCode}{(captionCode == -1 ? " (transparent)" : " (unexpected)")}";
        if (barCode != HtCaption) yield return "failed: the drag bar does not report as a caption, so it cannot be dragged";
        if (captionCode != -1) yield return "failed: the caption is not click-through, it would steal clicks from other applications";
    }

    /// <summary>
    /// How far the window may be dragged for the check. The gesture has to happen inside the
    /// virtual screen, so the window may not run off the right or the bottom edge: a user's drag
    /// is bounded by the screen in exactly the same way.
    /// </summary>
    private static System.Drawing.Point? GesturePlan(Window overlay, System.Drawing.Point start)
    {
        int width = Math.Max(1, GetSystemMetrics(0)), height = Math.Max(1, GetSystemMetrics(1));
        double left = SystemParameters.VirtualScreenLeft, top = SystemParameters.VirtualScreenTop;
        int rightLimit = (int)Math.Min(start.X + 240, left + SystemParameters.VirtualScreenWidth - width);
        int bottomLimit = (int)Math.Min(start.Y + 240, top + SystemParameters.VirtualScreenHeight - 60);
        var target = new System.Drawing.Point(Math.Max(start.X + 40, rightLimit - 40), Math.Max(start.Y + 40, bottomLimit - 40));
        if (target.X - start.X < 20 || target.Y - start.Y < 20) return null;
        return target;
    }

    /// <summary>
    /// Drags the bar with real mouse input and checks where the window ended up. This is the
    /// only way to prove the drag works: the window is layered, so UI automation cannot click
    /// it, and a programmatic move would not go through the caption drag at all.
    /// </summary>
    private static IEnumerable<string> Drag(IntPtr handle, Window overlay, string root)
    {
        var stateFile = Path.Combine(root, "outputs", "overlay.json");
        double beforeLeft = overlay.Left, beforeTop = overlay.Top;
        var start = PointToScreen(overlay, 60, 8);
        var target = GesturePlan(overlay, start);
        if (target is null)
        {
            yield return $"drag skipped: no room for the gesture (screen {GetSystemMetrics(0)}x{GetSystemMetrics(1)}, bar at {start.X},{start.Y})";
            yield break;
        }
        try { if (File.Exists(stateFile)) File.Delete(stateFile); }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }

        GetCursorPos(out var previous);
        SetForegroundWindow(handle);
        // A real press on the bar, held down while the cursor is moved: this is the only way to
        // exercise the caption drag without a person at the keyboard. The bar has to answer
        // WM_NCHITTEST with HTCAPTION or nothing here would move the window.
        SetCursorPos(start.X, start.Y);
        Press(start.X, start.Y);
        for (int step = 1; step <= 8; step++)
        {
            Thread.Sleep(25);
            AbsoluteMove(start.X + (target.Value.X - start.X) * step / 8, start.Y + (target.Value.Y - start.Y) * step / 8);
        }
        Thread.Sleep(60);
        ReleaseMouse(target.Value.X, target.Value.Y);
        // WM_EXITSIZEMOVE writes the position once the drag loop ends.
        for (int i = 0; i < 40 && !File.Exists(stateFile); i++) Pump(overlay, TimeSpan.FromMilliseconds(50));
        SetCursorPos(previous.X, previous.Y);

        double movedX = overlay.Left - beforeLeft, movedY = overlay.Top - beforeTop;
        string saved = Read(stateFile);
        yield return $"drag from=({start.X},{start.Y}) to=({target.Value.X},{target.Value.Y}) moved=({movedX:F0},{movedY:F0}) saved=\"{saved}\"";
        if (Math.Abs(movedX) < 10 && Math.Abs(movedY) < 10)
            yield return "failed: the window did not move when the bar was dragged";
        else if (saved.Length == 0)
            yield return "failed: the new position was not written to overlay.json";
    }

    /// <summary>An absolute mouse move, which is what the drag loop listens to.</summary>
    private static void AbsoluteMove(int x, int y)
    {
        int width = Math.Max(1, GetSystemMetrics(0) - 1), height = Math.Max(1, GetSystemMetrics(1) - 1);
        mouse_event(MouseeventfMove | MouseeventfAbsolute, (uint)(x * 65535 / width), (uint)(y * 65535 / height), 0, UIntPtr.Zero);
    }

    /// <summary>Presses the left button at the point, which is what starts the caption drag.</summary>
    private static void Press(int x, int y)
    {
        int width = Math.Max(1, GetSystemMetrics(0) - 1), height = Math.Max(1, GetSystemMetrics(1) - 1);
        mouse_event(MouseeventfMove | MouseeventfAbsolute | MouseeventfLeftdown, (uint)(x * 65535 / width), (uint)(y * 65535 / height), 0, UIntPtr.Zero);
    }

    private static void ReleaseMouse(int x, int y)
    {
        int width = Math.Max(1, GetSystemMetrics(0) - 1), height = Math.Max(1, GetSystemMetrics(1) - 1);
        mouse_event(MouseeventfMove | MouseeventfAbsolute | MouseeventfLeftup, (uint)(x * 65535 / width), (uint)(y * 65535 / height), 0, UIntPtr.Zero);
    }

    private static bool IsOnScreen(System.Drawing.Point point)
    {
        double left = SystemParameters.VirtualScreenLeft, top = SystemParameters.VirtualScreenTop;
        double right = left + SystemParameters.VirtualScreenWidth, bottom = top + SystemParameters.VirtualScreenHeight;
        return point.X >= left + 4 && point.Y >= top + 4 && point.X <= right - 4 && point.Y <= bottom - 4;
    }

    private static System.Drawing.Point PointToScreen(Window overlay, double x, double y)
    {
        var point = overlay.PointToScreen(new Point(x, y));
        return new System.Drawing.Point((int)Math.Round(point.X), (int)Math.Round(point.Y));
    }

    private static string Read(string path)
    {
        try { return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8).Trim() : ""; }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { return ""; }
    }

    private static int Hit(IntPtr handle, System.Drawing.Point point)
    {
        SendMessageTimeout(handle, WmNchittest, IntPtr.Zero, Pack(point.X, point.Y), SmtoAbortIfHung, 500, out var result);
        return result.ToInt32();
    }

    private static IntPtr Pack(int x, int y) => new((y << 16) | (x & 0xFFFF));

    /// <summary>
    /// The opacity sliders. The menu itself cannot be clicked without a person, so the same
    /// setters the sliders use are called here and the resulting brushes and the saved file are
    /// checked: the two things that decide what the user sees and what survives a restart.
    /// </summary>
    private static IEnumerable<string> Opacity(Window overlay, string root)
    {
        var window = (SubtitleOverlayWindow)overlay;
        var stateFile = Path.Combine(root, "outputs", "overlay.json");
        window.SetBackgroundOpacity(0.5);
        window.SetTextOpacity(0.5);
        Pump(overlay, TimeSpan.FromMilliseconds(150));
        var plate = ((SolidColorBrush)window.Shell.Background).Color;
        var text = ((SolidColorBrush)window.Caption.Foreground).Color;
        yield return $"opacity plate_alpha={plate.A} shell_opacity={window.Shell.Opacity:F2} text_alpha={text.A} caption_opacity={window.Caption.Opacity:F2}";
        if (plate.A >= 0xD9 || plate.A == 0) yield return "failed: the background slider did not change the plate";
        if (Math.Abs(window.Caption.Opacity - 0.5) > 0.01) yield return "failed: the text slider did not change the caption";
        string saved = Read(stateFile);
        yield return $"opacity saved=\"{saved}\"";
        if (!saved.Contains("0.5", StringComparison.Ordinal)) yield return "failed: the opacities were not written to overlay.json";
        // Back to the defaults, so the check does not leave the strip see-through. 0.85 of the
        // plate's alpha is 184, not the original 217: the default is a percentage of that alpha.
        window.SetBackgroundOpacity(0.85);
        window.SetTextOpacity(1);
        Pump(overlay, TimeSpan.FromMilliseconds(150));
        var restored = ((SolidColorBrush)window.Shell.Background).Color;
        yield return $"opacity restored plate_alpha={restored.A} shell_opacity={window.Shell.Opacity:F2} text_opacity={window.Caption.Opacity:F2}";
        if (Math.Abs(window.Shell.Opacity - 0.85) > 0.01 || Math.Abs(window.Caption.Opacity - 1) > 0.01)
            yield return "failed: the opacities did not return to their defaults";
    }

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

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(IntPtr handle, int message, IntPtr wParam, IntPtr lParam, int flags, int timeout, out IntPtr result);

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out System.Drawing.Point point);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

    [DllImport("user32.dll")]
    private static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr handle);
}
