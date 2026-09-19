using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;

namespace LiveSub.Launcher;

/// <summary>
/// The live subtitle as an always-on-top, click-through strip over whatever the user is
/// running. The window itself never activates and never takes mouse input; only the thin bar
/// at the top answers the mouse, so the caption stays visible while the user keeps working in
/// another application.
/// </summary>
public partial class SubtitleOverlayWindow : Window
{
    private const int GwlExstyle = -20;
    private const int WsExTransparent = 0x00000020;
    private const int WsExLayered = 0x00080000;
    private const int WsExNoActivate = 0x08000000;
    private const int WsExToolWindow = 0x00000080;
    private const double ClearAfterSeconds = 15;

    private readonly DispatcherTimer _dwell = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly string _stateFile;
    private string _text = "";
    private double _idle;
    private bool _dragging;
    private Point _grab;
    private bool _placed;

    public SubtitleOverlayWindow(string root)
    {
        InitializeComponent();
        _stateFile = Path.Combine(root, "outputs", "overlay.json");
        _dwell.Tick += (_, _) =>
        {
            // A pause in speech must not leave the last line on screen forever, but it must
            // also not blink away between two sentences, so the caption is cleared by time
            // rather than by the next subtitle arriving.
            _idle += 1;
            if (_idle >= ClearAfterSeconds && Caption.Text.Length > 0)
            {
                Caption.Text = "";
                BarHint.Text = "LiveSub 悬浮字幕 · 等待语音";
            }
        };
        _dwell.Start();
        Loaded += (_, _) => { RestorePosition(); ApplyClickThrough(); };
        Closed += (_, _) => _dwell.Stop();
    }

    public string Current => _text;

    public void Show(string text)
    {
        _text = text;
        _idle = 0;
        Caption.Text = text;
        BarHint.Text = "LiveSub 悬浮字幕 · 拖动此处移动";
        if (!IsVisible) Show();
    }

    private void RestorePosition()
    {
        try
        {
            if (File.Exists(_stateFile))
            {
                var parts = File.ReadAllText(_stateFile).Split(',');
                if (parts.Length == 2
                    && double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out double left)
                    && double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out double top))
                {
                    MoveTo(left, top);
                    _placed = true;
                }
            }
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
        if (!_placed) CenterNearBottom();
    }

    private void CenterNearBottom()
    {
        var area = SystemParameters.WorkArea;
        UpdateLayout();
        Left = area.Left + Math.Max(0, (area.Width - ActualWidth) / 2);
        Top = area.Top + Math.Max(0, area.Height * 0.78 - ActualHeight);
    }

    private void MoveTo(double left, double top)
    {
        var area = SystemParameters.WorkArea;
        Left = Math.Min(Math.Max(left, area.Left - 40), area.Left + Math.Max(0, area.Width - 120));
        Top = Math.Min(Math.Max(top, area.Top), area.Top + Math.Max(0, area.Height - 40));
    }

    private void ApplyClickThrough()
    {
        var handle = new WindowInteropHelper(this).Handle;
        if (handle == IntPtr.Zero) return;
        long style = GetWindowLongPtr(handle, GwlExstyle).ToInt64();
        style |= WsExTransparent | WsExLayered | WsExNoActivate | WsExToolWindow;
        SetWindowLongPtr(handle, GwlExstyle, new IntPtr(style));
    }

    private void SavePosition()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_stateFile)!);
            File.WriteAllText(_stateFile, string.Create(CultureInfo.InvariantCulture, $"{Left},{Top}"));
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
    }

    private void DragBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        _dragging = true;
        _grab = e.GetPosition(this);
        DragBar.CaptureMouse();
        e.Handled = true;
    }

    private void DragBar_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_dragging) return;
        var point = PointToScreen(e.GetPosition(this));
        MoveTo(point.X - _grab.X, point.Y - _grab.Y);
    }

    private void DragBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (!_dragging) return;
        _dragging = false;
        DragBar.ReleaseMouseCapture();
        SavePosition();
        e.Handled = true;
    }

    private void Copy_Click(object sender, RoutedEventArgs e)
    {
        if (_text.Length == 0) return;
        try { Clipboard.SetText(_text); }
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
