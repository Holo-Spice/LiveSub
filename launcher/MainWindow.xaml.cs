using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Navigation;
using System.Windows.Shapes;

namespace LiveSub.Launcher;

/// <summary>
/// Interaction logic for MainWindow.xaml
/// </summary>
public partial class MainWindow : Window
{
    private bool _closingAfterStop;

    public MainWindow(string? existingRoot = null)
    {
        InitializeComponent();
        if (existingRoot is not null)
        {
            ShowSubtitle(existingRoot, existing: true);
            return;
        }
        var current = System.IO.Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory);
        var statePath = System.IO.Path.Combine(current, "install-state.json");
        if (InstallState.IsReady(current))
            ShowSubtitle(current);
        else
        {
            var proposed = System.IO.File.Exists(statePath)
                ? current
                : System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LiveSub");
            Setup.Begin(proposed);
            Setup.Installed += (_, root) => ShowSubtitle(root);
            Setup.Visibility = Visibility.Visible;
        }
    }

    private void ShowSubtitle(string root, bool existing = false)
    {
        Setup.Visibility = Visibility.Collapsed;
        Subtitle.Begin(root, existing);
        Subtitle.Visibility = Visibility.Visible;
    }

    private async void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_closingAfterStop || (!Setup.IsBusy && !Subtitle.IsRunning))
            return;
        e.Cancel = true;
        IsEnabled = false;
        await Setup.StopAsync();
        await Subtitle.StopAsync();
        _closingAfterStop = true;
        Close();
    }
}
