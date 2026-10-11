// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace RepoPulse;

public sealed class Settings
{
    public string Roots { get; set; } = "";
    public string BackupDir { get; set; } = "";
    public bool IncludeCaches { get; set; } = true;

    static string FilePath => System.IO.Path.Combine(AppContext.BaseDirectory, "data", "settings.json");

    public static Settings Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<Settings>(File.ReadAllText(FilePath)) ?? new Settings();
        }
        catch { }
        return new Settings();
    }

    public void Save()
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
    }
}

/// <summary>Runs repopulse.py and returns its single JSON object.</summary>
public static class Core
{
    static string Root => AppContext.BaseDirectory;
    static (string File, string[] Prefix)? python;

    static (string File, string[] Prefix) Python()
    {
        if (python is { } cached) return cached;
        var venv = System.IO.Path.Combine(Root, ".venv", "Scripts", "python.exe");
        if (File.Exists(venv)) return (python = (venv, Array.Empty<string>())).Value;
        foreach (var (file, prefix) in new[] { ("py", new[] { "-3" }), ("python", Array.Empty<string>()) })
        {
            try
            {
                var psi = new ProcessStartInfo(file) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                foreach (var p in prefix) psi.ArgumentList.Add(p);
                psi.ArgumentList.Add("-c");
                psi.ArgumentList.Add("import sys; print(sys.version_info >= (3, 10))");
                using var proc = Process.Start(psi);
                if (proc == null) continue;
                var output = proc.StandardOutput.ReadToEnd();
                if (proc.WaitForExit(8000) && proc.ExitCode == 0 && output.Trim() == "True")
                    return (python = (file, prefix)).Value;
            }
            catch { }
        }
        throw new InvalidOperationException("Python 3.10 or newer was not found. Install it from python.org with the Python launcher, then restart Repo Pulse.");
    }

    public static async Task<JsonElement> Run(TimeSpan timeout, params string[] args)
    {
        var (file, prefix) = Python();
        var psi = new ProcessStartInfo(file)
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Root,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
        };
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        foreach (var p in prefix) psi.ArgumentList.Add(p);
        psi.ArgumentList.Add(System.IO.Path.Combine(Root, "repopulse.py"));
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("Could not start Python.");
        var stdout = proc.StandardOutput.ReadToEndAsync();
        var stderr = proc.StandardError.ReadToEndAsync();
        using var cts = new CancellationTokenSource(timeout);
        try { await proc.WaitForExitAsync(cts.Token); }
        catch (OperationCanceledException)
        {
            try { proc.Kill(true); } catch { }
            throw new TimeoutException($"Repo Pulse core did not finish within {timeout.TotalMinutes:0} minutes.");
        }
        var text = (await stdout).Trim();
        var line = text.Split('\n').LastOrDefault(l => l.TrimStart().StartsWith("{"));
        if (line == null)
            throw new InvalidOperationException("Core returned no result. " + (await stderr).Trim());
        using var doc = JsonDocument.Parse(line);
        return doc.RootElement.Clone();
    }
}

public static class J
{
    public static string S(this JsonElement e, string name, string fallback = "") =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? fallback : fallback;

    public static long N(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? (long)v.GetDouble() : 0;

    public static double? D(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

    public static bool B(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    public static bool Has(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind != JsonValueKind.Null;

    public static JsonElement O(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) ? v : default;

    public static IEnumerable<JsonElement> A(this JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Array ? v.EnumerateArray() : Enumerable.Empty<JsonElement>();

    public static string Human(long bytes)
    {
        string[] units = { "B", "KB", "MB", "GB", "TB" };
        double size = bytes;
        int i = 0;
        while (size >= 1024 && i < units.Length - 1) { size /= 1024; i++; }
        return i == 0 ? $"{bytes} B" : $"{size:0.0} {units[i]}";
    }

    public static string Ago(long unix)
    {
        if (unix <= 0) return "never";
        var span = DateTimeOffset.UtcNow - DateTimeOffset.FromUnixTimeSeconds(unix);
        if (span.TotalMinutes < 60) return $"{Math.Max(1, (int)span.TotalMinutes)} min ago";
        if (span.TotalHours < 48) return $"{(int)span.TotalHours} h ago";
        if (span.TotalDays < 60) return $"{(int)span.TotalDays} d ago";
        return $"{(int)(span.TotalDays / 30)} mo ago";
    }
}
