// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using static RepoPulse.Ui;

namespace RepoPulse;

public sealed class DiffWindow : Window
{
    const int MaxLines = 4000;

    public DiffWindow(string file, string text, bool truncated)
    {
        Title = "Diff — " + file;
        Width = 760; Height = 600; MinWidth = 360; MinHeight = 240;
        ResizeMode = ResizeMode.CanResizeWithGrip;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        MakeGlass(this);

        var lines = new StackPanel();
        var all = text.Replace("\r\n", "\n").Split('\n');
        foreach (var line in all.Take(MaxLines))
        {
            Brush fg = line.StartsWith("+") && !line.StartsWith("+++") ? new SolidColorBrush(Good)
                     : line.StartsWith("-") && !line.StartsWith("---") ? new SolidColorBrush(Bad)
                     : line.StartsWith("@@") ? new SolidColorBrush(Info)
                     : Dim;
            lines.Children.Add(new TextBlock { Text = line.Length == 0 ? " " : line, FontFamily = Mono, FontSize = 12, Foreground = fg });
        }
        if (truncated || all.Length > MaxLines)
            lines.Children.Add(T("… diff truncated", 12, Dim));

        var close = Pill("Close", () => { Close(); return Task.CompletedTask; });
        var header = new DockPanel { Background = Brushes.Transparent, Margin = new Thickness(0, 0, 0, 8) };
        DockPanel.SetDock(close, Dock.Right);
        header.Children.Add(close);
        header.Children.Add(T(file, 14, Text, bold: true));
        header.MouseLeftButtonDown += (_, e) => { if (e.ButtonState == MouseButtonState.Pressed) DragMove(); };

        var scroll = new ScrollViewer
        {
            Content = lines, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        };
        var dock = new DockPanel();
        DockPanel.SetDock(header, Dock.Top);
        dock.Children.Add(header);
        dock.Children.Add(scroll);
        Content = GlassShell(dock);
        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
    }
}

public sealed class SettingsWindow : Window
{
    public SettingsWindow(Settings settings)
    {
        Title = "Repo Pulse settings";
        Width = 520; SizeToContent = SizeToContent.Height;
        ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        MakeGlass(this);

        var roots = Box(settings.Roots);
        var backup = Box(settings.BackupDir);
        var caches = new CheckBox { IsChecked = settings.IncludeCaches, Margin = new Thickness(0, 10, 0, 0),
                                    Content = T("Include global package caches (npm, pip, Yarn, pnpm, NuGet, Playwright)", 12) };

        var body = new StackPanel();
        body.Children.Add(T("Settings", 16, Text, bold: true));
        body.Children.Add(Hint("Folders to scan (separate with ;). Empty = every fixed drive. Folders known to Claude Code, Codex and VS Code, and every linked worktree, are always included."));
        body.Children.Add(roots);
        body.Children.Add(Hint("Backup folder. Empty = %USERPROFILE%\\RepoPulse Backups"));
        body.Children.Add(backup);
        body.Children.Add(caches);
        var buttons = Row(
            Pill("Save", () =>
            {
                settings.Roots = roots.Text.Trim();
                settings.BackupDir = backup.Text.Trim();
                settings.IncludeCaches = caches.IsChecked == true;
                DialogResult = true;
                return Task.CompletedTask;
            }, primary: true),
            Pill("Cancel", () => { DialogResult = false; return Task.CompletedTask; }));
        buttons.Margin = new Thickness(0, 14, 0, 0);
        body.Children.Add(buttons);
        body.MouseLeftButtonDown += (_, e) => { if (e.ButtonState == MouseButtonState.Pressed && e.OriginalSource is not TextBox) DragMove(); };
        Content = GlassShell(body);
    }

    static TextBlock Hint(string text)
    {
        var t = T(text, 12, Dim, wrap: true);
        t.Margin = new Thickness(0, 12, 0, 4);
        return t;
    }

    static TextBox Box(string value) => new()
    {
        Text = value, FontSize = 13, Padding = new Thickness(6, 4, 6, 4),
        Background = Br("#33FFFFFF"), Foreground = Ui.Text, CaretBrush = Accent, BorderBrush = Line,
    };
}
