// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace RepoPulse;

/// <summary>Small glass-style building blocks shared by the windows (Prism palette).</summary>
public static class Ui
{
    public static Color C(string hex) => (Color)ColorConverter.ConvertFromString(hex);
    public static SolidColorBrush Br(string hex) { var b = new SolidColorBrush(C(hex)); b.Freeze(); return b; }

    public static readonly SolidColorBrush Text = Br("#E5EFFC");
    public static readonly SolidColorBrush Dim = Br("#9FB2CC");
    public static readonly SolidColorBrush Accent = Br("#89E6D5");
    public static readonly SolidColorBrush Panel = Br("#B31B2B42");
    public static readonly SolidColorBrush PanelHover = Br("#CC26395A");
    public static readonly SolidColorBrush Line = Br("#332A3D57");
    public static readonly Color Good = C("#6EE7A8"), Warn = C("#F5C86B"), Bad = C("#FF8A8A"), Info = C("#A9D2FF"), Muted = C("#7F93AE");
    public static readonly FontFamily Mono = new("Cascadia Mono, Consolas");

    public static TextBlock T(string text, double size = 13, Brush? fg = null, bool bold = false, bool wrap = false) => new()
    {
        Text = text, FontSize = size, Foreground = fg ?? Text, FontWeight = bold ? FontWeights.SemiBold : FontWeights.Normal,
        TextWrapping = wrap ? TextWrapping.Wrap : TextWrapping.NoWrap, TextTrimming = wrap ? TextTrimming.None : TextTrimming.CharacterEllipsis,
        VerticalAlignment = VerticalAlignment.Center,
    };

    public static Border Chip(string text, Color color) => new()
    {
        Background = new SolidColorBrush(Color.FromArgb(0x33, color.R, color.G, color.B)),
        BorderBrush = new SolidColorBrush(Color.FromArgb(0x88, color.R, color.G, color.B)), BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(9), Padding = new Thickness(7, 1, 7, 2), Margin = new Thickness(0, 0, 6, 0),
        VerticalAlignment = VerticalAlignment.Center,
        Child = new TextBlock { Text = text, FontSize = 11, Foreground = new SolidColorBrush(color), FontWeight = FontWeights.SemiBold },
    };

    /// <summary>A pill button. Handles its own mouse-down so the window drag never swallows the click.</summary>
    public static Border Pill(string text, Func<Task> action, bool primary = false, string? tip = null)
    {
        var normal = primary ? Br("#4D89E6D5") : Br("#26FFFFFF");
        var hover = primary ? Br("#8089E6D5") : Br("#40FFFFFF");
        var pill = new Border
        {
            Background = normal, CornerRadius = new CornerRadius(11), Padding = new Thickness(10, 3, 10, 4), Margin = new Thickness(0, 0, 6, 0),
            Cursor = Cursors.Hand, ToolTip = tip, VerticalAlignment = VerticalAlignment.Center,
            Child = new TextBlock { Text = text, FontSize = 12, Foreground = primary ? Accent : Text },
        };
        pill.MouseEnter += (_, _) => pill.Background = hover;
        pill.MouseLeave += (_, _) => pill.Background = normal;
        pill.MouseLeftButtonDown += (_, e) => e.Handled = true;
        pill.MouseLeftButtonUp += async (_, e) =>
        {
            e.Handled = true;
            if (!pill.IsEnabled) return;
            pill.IsEnabled = false;
            pill.Opacity = 0.5;
            try { await action(); }
            finally { pill.IsEnabled = true; pill.Opacity = 1; }
        };
        return pill;
    }

    public static Border Card(UIElement child) => new()
    {
        Background = Panel, CornerRadius = new CornerRadius(12), Padding = new Thickness(12, 10, 12, 10),
        Margin = new Thickness(0, 0, 0, 8), Child = child, BorderBrush = Line, BorderThickness = new Thickness(1),
    };

    public static StackPanel Row(params UIElement[] children)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 2) };
        foreach (var c in children) row.Children.Add(c);
        return row;
    }

    public static Border GlassShell(UIElement child) => new()
    {
        CornerRadius = new CornerRadius(18), Margin = new Thickness(10), Padding = new Thickness(16, 12, 16, 12),
        Background = new LinearGradientBrush(C("#EE142235"), C("#EE0B1524"), 90),
        BorderBrush = new SolidColorBrush(C("#5589E6D5")), BorderThickness = new Thickness(1),
        Effect = new System.Windows.Media.Effects.DropShadowEffect { BlurRadius = 26, ShadowDepth = 0, Opacity = 0.55 },
        Child = child,
    };

    public static void MakeGlass(Window w)
    {
        w.WindowStyle = WindowStyle.None;
        w.AllowsTransparency = true;
        w.Background = Brushes.Transparent;
        w.FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI");
    }
}
