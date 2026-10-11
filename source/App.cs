// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Windows;

namespace RepoPulse;

public sealed class App : Application
{
    [STAThread]
    public static void Main()
    {
        using var mutex = new Mutex(true, "Local\\RepoPulse_SingleInstance", out bool created);
        if (!created)
        {
            MessageBox.Show("Repo Pulse is already running.", "Repo Pulse", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        var app = new App { ShutdownMode = ShutdownMode.OnMainWindowClose };
        app.DispatcherUnhandledException += (_, e) =>
        {
            MessageBox.Show(e.Exception.Message, "Repo Pulse error", MessageBoxButton.OK, MessageBoxImage.Error);
            e.Handled = true;
        };
        app.Run(new MainWindow());
    }
}
