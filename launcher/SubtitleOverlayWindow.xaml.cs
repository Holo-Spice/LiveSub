using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace LiveSub.Launcher;

/// <summary>
/// The live subtitle as an always-on-top strip over whatever the user is running.
///
/// Three things have to be true at the same time and they are all handled here:
///
/// * The caption must not steal a click from the application the user is working in. Only the
///   thin bar at the top is part of hit testing (<c>WM_NCHITTEST</c> answers <c>HTCAPTION</c>
///   there and <c>HTTRANSPARENT</c> everywhere else). The whole-window
///   <c>WS_EX_TRANSPARENT</c> style that used to do this also swallowed the window's own mouse
///   messages, which is why the drag bar could not be grabbed at all.
/// * The bar drags the window. With the bar answering <c>HTCAPTION</c> the window manager does
///   the dragging, so no mouse capture, no activation and no coordinate arithmetic of ours is
///   involved.
/// * The strip can be made see-through. The background and the text have separate opacity
///   sliders in the right-click menu, because how much black is needed depends on the video
///   behind the subtitle and cannot be guessed here.
///
/// Lines handed in by <see cref="Show"/> are queued instead of being drawn immediately: one
/// utterance usually carries several lines and they arrive together, and drawing them all at
/// once made the first, longest line unreadable. <see cref="OverlaySchedule"/> decides the
/// timing.
/// </summary>
public partial class SubtitleOverlayWindow : Window
{
    private const int GwlExstyle = -20;
    private const int WsExLayered = 0x00080000;
    private const int WsExNoActivate = 0x08000000;
    private const int WsExToolWindow = 0x00000080;

    /// <summary>
    /// Hit testing. The drag bar reports as a caption, so the window manager drags the window;
    /// everywhere else reports transparent, so the click belongs to the window below.
    /// </summary>
    private const int WmNchittest = 0x0084;
    private const int HtTransparent = -1;
    private const int HtCaption = 2;

    private const int WmExitsizemove = 0x0232;
    private const string ReadyHint = "LiveSub 悬浮字幕 · 拖动此处移动位置（右键菜单）";
    private const string WaitingHint = "LiveSub 悬浮字幕 · 等待语音";

    /// <summary>
    /// Opacity of the background plate and of the text, as the user set them. The background
    /// default is the alpha the plate always had; the text keeps a floor so a caption can never
    /// be made completely unreadable by accident.
    /// </summary>
    private const double DefaultBackgroundOpacity = 0.85;
    private const double MinTextOpacity = 0.1;

    /// <summary>The plate's original alpha, which the opacity slider scales.</summary>
    private const byte BackgroundAlpha = 0xD9;
    private const byte TextAlpha = 0xFF;

    /// <summary>
    /// 10 Hz: fast enough that a line appears when it is due, slow enough to cost nothing.
    /// The timing rules themselves live in <see cref="OverlaySchedule"/>.
    /// </summary>
    private static readonly TimeSpan TickInterval = TimeSpan.FromMilliseconds(100);

    private readonly DispatcherTimer _tick = new() { Interval = TickInterval };
    private readonly OverlaySchedule _schedule = new();
    private readonly string _stateFile;
    private double _backgroundOpacity = DefaultBackgroundOpacity;
    private double _textOpacity = 1;
    private long _ticks;
    private long _seen;
    private bool _placed;
    private bool _restoring;

    public SubtitleOverlayWindow(string root)
    {
        InitializeComponent();
        _stateFile = Path.Combine(root, "outputs", "overlay.json");
        _tick.Tick += (_, _) => OnTick();
        _tick.Start();
        Loaded += (_, _) =>
        {
            // Restoring assigns the sliders as well, so the menu opens on the saved values.
            _restoring = true;
            Restore();
            _restoring = false;
            ApplyOpacity();
        };
        Closed += (_, _) => _tick.Stop();
    }

    /// <summary>The line currently on screen, for the self-check and the context menu.</summary>
    public string Current => _schedule.Visible;

    /// <summary>
    /// Queues the lines of one utterance. Each entry is the text, its start and end on the
    /// media timeline in milliseconds, and whether the line may stay on screen after the
    /// utterance ends. The lines are shown in order, timed by their own duration, so a long
    /// line is never wiped by the short phrase that followed it.
    /// </summary>
    public void Show(IReadOnlyList<(string Text, long StartMs, long EndMs, bool Keep)> lines, long elapsedMs = 0)
    {
        _schedule.Push(lines, _ticks, elapsedMs);
        if (!IsVisible) Show();
    }

    /// <summary>A status line instead of a subtitle, e.g. while the models are still loading.</summary>
    public void ShowHint(string text)
    {
        _schedule.Push([(text, 0, 0, true)], _ticks);
        if (!IsVisible) Show();
    }

    /// <summary>
    /// The two opacities, as the menu sliders set them. Public because the self-check cannot
    /// click a context menu and has to drive the same values the sliders do.
    /// </summary>
    public double BackgroundOpacity => _backgroundOpacity;

    public double TextOpacity => _textOpacity;

    public void SetBackgroundOpacity(double value) => BackgroundSlider.Value = Math.Clamp(value, 0, 1) * 100;

    public void SetTextOpacity(double value) => TextSlider.Value = Math.Clamp(value, MinTextOpacity, 1) * 100;

    private void OnTick()
    {
        _ticks++;
        bool cleared = _schedule.Advance(_ticks);
        if (_schedule.Shown != _seen)
        {
            // A line was handed out: draw it. Nothing else in this tick can be newer.
            _seen = _schedule.Shown;
            Caption.Text = _schedule.Visible;
        }
        else if (cleared)
        {
            Caption.Text = "";
        }
        BarHint.Text = _schedule.Busy(_ticks) ? ReadyHint : WaitingHint;
    }

    /// <summary>
    /// Restores where the strip was and how see-through it was. One file and one line, so a
    /// value that cannot be read simply falls back to the default instead of failing the window.
    /// </summary>
    private void Restore()
    {
        var parts = ReadState();
        if (parts.Length >= 2
            && double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out double left)
            && double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out double top))
        {
            MoveTo(left, top);
            _placed = true;
        }
        _backgroundOpacity = ReadOpacity(parts, 2, DefaultBackgroundOpacity, 0);
        _textOpacity = ReadOpacity(parts, 3, 1, MinTextOpacity);
        BackgroundSlider.Value = _backgroundOpacity * 100;
        TextSlider.Value = _textOpacity * 100;
        UpdateOpacityLabels();
        if (!_placed) CenterNearBottom();
    }

    private string[] ReadState()
    {
        try
        {
            return File.Exists(_stateFile) ? File.ReadAllText(_stateFile).Split(',') : [];
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { return []; }
    }

    private static double ReadOpacity(string[] parts, int index, double fallback, double minimum)
    {
        if (parts.Length <= index
            || !double.TryParse(parts[index], NumberStyles.Float, CultureInfo.InvariantCulture, out double value)
            || !double.IsFinite(value)) return fallback;
        return Math.Clamp(value, minimum, 1);
    }

    /// <summary>
    /// The default place: centred, with the bottom edge of the caption near the bottom of the
    /// work area. The whole window has to stay inside the screen, because a window whose drag
    /// bar is off the bottom cannot be dragged back.
    /// </summary>
    private void CenterNearBottom()
    {
        var area = SystemParameters.WorkArea;
        UpdateLayout();
        double width = ActualWidth > 0 ? ActualWidth : Width;
        double height = ActualHeight > 0 ? ActualHeight : 120;
        Left = area.Left + Math.Max(0, (area.Width - width) / 2);
        Top = Math.Max(area.Top + 8, area.Top + area.Height * 0.86 - height);
        MoveTo(Left, Top);
    }

    /// <summary>Keeps a dragged or restored window reachable: the bar always stays on screen.</summary>
    private void MoveTo(double left, double top)
    {
        var area = SystemParameters.WorkArea;
        double width = ActualWidth > 0 ? ActualWidth : Width;
        Left = Math.Min(Math.Max(left, area.Left - width + 160), area.Left + area.Width - 120);
        Top = Math.Min(Math.Max(top, area.Top), area.Top + Math.Max(0, area.Height - 40));
    }

    /// <summary>Writes the place and the opacities together, so neither can be lost.</summary>
    private void SaveState()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_stateFile)!);
            var line = string.Create(
                CultureInfo.InvariantCulture,
                $"{Left},{Top},{_backgroundOpacity.ToString("0.###", CultureInfo.InvariantCulture)},{_textOpacity.ToString("0.###", CultureInfo.InvariantCulture)}");
            File.WriteAllText(_stateFile, line);
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
    }

    /// <summary>
    /// Applies both opacities. The background is applied twice on purpose: the plate's alpha and
    /// the element's opacity are multiplied together, so writing the alpha alone could never make
    /// the plate fully transparent.
    /// </summary>
    private void ApplyOpacity()
    {
        byte alpha = (byte)Math.Round(BackgroundAlpha * _backgroundOpacity);
        Shell.Background = new SolidColorBrush(Color.FromArgb(alpha, 0, 0, 0));
        Shell.Opacity = _backgroundOpacity;
        Caption.Foreground = new SolidColorBrush(Color.FromArgb(TextAlpha, 0xFF, 0xFF, 0xFF));
        Caption.Opacity = _textOpacity;
    }

    private void UpdateOpacityLabels()
    {
        BackgroundItem.Header = $"背景透明度 {_backgroundOpacity * 100:F0}%";
        TextItem.Header = $"字幕透明度 {_textOpacity * 100:F0}%";
    }

    private void BackgroundSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        _backgroundOpacity = Math.Clamp(e.NewValue / 100, 0, 1);
        UpdateOpacityLabels();
        ApplyOpacity();
        if (!_restoring) SaveState();
    }

    private void TextSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        _textOpacity = Math.Clamp(e.NewValue / 100, MinTextOpacity, 1);
        UpdateOpacityLabels();
        ApplyOpacity();
        if (!_restoring) SaveState();
    }

    private void ResetOpacity_Click(object sender, RoutedEventArgs e)
    {
        BackgroundSlider.Value = DefaultBackgroundOpacity * 100;
        TextSlider.Value = 100;
        SaveState();
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        var handle = new WindowInteropHelper(this).Handle;
        if (handle == IntPtr.Zero) return;
        // Deliberately not WS_EX_TRANSPARENT: that style would make the whole window - the drag
        // bar included - invisible to the mouse. Hit testing is answered per region instead.
        long style = GetWindowLongPtr(handle, GwlExstyle).ToInt64();
        style |= WsExLayered | WsExNoActivate | WsExToolWindow;
        SetWindowLongPtr(handle, GwlExstyle, new IntPtr(style));
        HwndSource.FromHwnd(handle)?.AddHook(Hook);
    }

    private IntPtr Hook(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == WmNchittest)
        {
            handled = true;
            return new IntPtr(HitTest(lParam));
        }
        if (message == WmExitsizemove)
        {
            SaveState();
        }
        return IntPtr.Zero;
    }

    /// <summary>
    /// The bar answers as a caption so the window manager drags the window; everything else
    /// answers as transparent so the click goes to the application underneath.
    /// </summary>
    private int HitTest(IntPtr lParam)
    {
        int packed = unchecked((int)lParam.ToInt64());
        double x = unchecked((short)(packed & 0xFFFF));
        double y = unchecked((short)((packed >> 16) & 0xFFFF));
        var local = PointFromScreen(new Point(x, y));
        if (local.X < 0 || local.Y < 0 || local.X >= ActualWidth || local.Y >= ActualHeight) return HtTransparent;
        return local.Y <= DragBar.ActualHeight ? HtCaption : HtTransparent;
    }

    private void Copy_Click(object sender, RoutedEventArgs e)
    {
        if (Current.Length == 0) return;
        try { Clipboard.SetText(Current); }
        catch (COMException) { }
    }

    private void Topmost_Click(object sender, RoutedEventArgs e)
    {
        Topmost = !Topmost;
        TopItem.Header = Topmost ? "取消置顶" : "保持置顶";
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr(IntPtr handle, int index);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtr(IntPtr handle, int index, IntPtr value);
}
