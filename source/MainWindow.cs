// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using static RepoPulse.Ui;

namespace RepoPulse;

public sealed class MainWindow : Window
{
    readonly Settings settings = Settings.Load();
    readonly StackPanel list = new();
    readonly TextBlock summary = T("Scanning…", 12, Dim);
    readonly TextBlock footer = T("", 11, Dim, wrap: true);
    readonly Border tabStatus, tabSpace;
    readonly HashSet<string> expanded = new();
    // repo path ("" for global caches) -> selected item ids
    readonly Dictionary<string, HashSet<string>> selected = new();
    JsonElement scan;
    bool hasScan, busy;
    string tab = "status";

    static readonly TimeSpan ScanTimeout = TimeSpan.FromMinutes(15);

    public MainWindow()
    {
        Title = "Repo Pulse";
        Width = 500; Height = 720; MinWidth = 400; MinHeight = 420;
        ResizeMode = ResizeMode.CanResizeWithGrip;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        MakeGlass(this);

        var title = Row(T("◆ ", 16, Accent), T("Repo Pulse", 16, Text, bold: true));
        var actions = Row(
            Pill("Scan", () => Scan(false), primary: true, tip: "Read-only scan of local state"),
            Pill("Fetch + scan", () => Scan(true), tip: "git fetch --prune each repo first (updates remote-tracking refs only)"),
            Pill("⚙", OpenSettings, tip: "Scan folders and backup location"),
            Pill("–", () => { WindowState = WindowState.Minimized; return Task.CompletedTask; }),
            Pill("×", () => { Close(); return Task.CompletedTask; }));
        var header = new DockPanel { LastChildFill = false, Background = Brushes.Transparent };
        DockPanel.SetDock(actions, Dock.Right);
        header.Children.Add(actions);
        header.Children.Add(title);
        header.MouseLeftButtonDown += (_, e) => { if (e.ButtonState == MouseButtonState.Pressed) DragMove(); };

        tabStatus = Tab("Status", "status");
        tabSpace = Tab("Space", "space");
        var tabs = Row(tabStatus, tabSpace);
        tabs.Margin = new Thickness(0, 8, 0, 8);

        var scroller = new ScrollViewer { Content = list, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(0, 0, 4, 0) };
        var top = new StackPanel();
        top.Children.Add(header);
        top.Children.Add(summary);
        top.Children.Add(tabs);
        var dock = new DockPanel();
        DockPanel.SetDock(top, Dock.Top);
        DockPanel.SetDock(footer, Dock.Bottom);
        footer.Margin = new Thickness(0, 6, 0, 0);
        dock.Children.Add(top);
        dock.Children.Add(footer);
        dock.Children.Add(scroller);
        Content = GlassShell(dock);
        RefreshTabs();
        Loaded += async (_, _) => await Scan(false);
    }

    Border Tab(string text, string key)
    {
        var b = new Border { CornerRadius = new CornerRadius(10), Padding = new Thickness(14, 4, 14, 5), Margin = new Thickness(0, 0, 6, 0), Cursor = Cursors.Hand,
                             Child = T(text, 13) };
        b.MouseLeftButtonDown += (_, e) => e.Handled = true;
        b.MouseLeftButtonUp += (_, _) => { tab = key; RefreshTabs(); Render(); };
        return b;
    }

    void RefreshTabs()
    {
        tabStatus.Background = tab == "status" ? Br("#4089E6D5") : Br("#14FFFFFF");
        tabSpace.Background = tab == "space" ? Br("#4089E6D5") : Br("#14FFFFFF");
    }

    void Say(string text) => footer.Text = text;

    // ------------------------------------------------------------ core calls

    async Task Scan(bool fetch)
    {
        if (busy) return;
        busy = true;
        Say(fetch ? "Fetching and scanning…" : "Scanning…");
        try
        {
            var args = new List<string> { "scan" };
            if (!string.IsNullOrWhiteSpace(settings.Roots)) { args.Add("--roots"); args.Add(settings.Roots); }
            if (settings.IncludeCaches) args.Add("--caches");
            if (fetch) args.Add("--fetch");
            var result = await Core.Run(ScanTimeout, args.ToArray());
            if (!result.B("ok")) { Say("Scan failed: " + result.S("error")); return; }
            scan = result;
            hasScan = true;
            selected.Clear();
            Render();
            Say($"Scanned {DateTime.Now:HH:mm}. Nothing was changed.");
        }
        catch (Exception ex) { Say(ex.Message); }
        finally { busy = false; }
    }

    async Task<JsonElement?> Act(string busyText, params string[] args)
    {
        if (busy) { Say("Busy; wait for the current task."); return null; }
        busy = true;
        Say(busyText);
        try { return await Core.Run(TimeSpan.FromMinutes(30), args); }
        catch (Exception ex) { Say(ex.Message); return null; }
        finally { busy = false; }
    }

    string[] WithDest(params string[] args) =>
        string.IsNullOrWhiteSpace(settings.BackupDir) ? args : args.Concat(new[] { "--dest", settings.BackupDir }).ToArray();

    // ------------------------------------------------------------ rendering

    void Render()
    {
        list.Children.Clear();
        if (!hasScan) return;
        var totals = scan.O("totals");
        summary.Text = $"{totals.N("repos")} repos · {totals.N("needs_attention")} need attention · " +
                       $"{J.Human(totals.N("safe_bytes"))} safe to reclaim · {J.Human(totals.N("review_bytes"))} to review";
        if (tab == "status") RenderStatus(); else RenderSpace();
        if (list.Children.Count == 0)
            list.Children.Add(T("No git repositories found. Add folders in ⚙ settings.", 13, Dim, wrap: true));
    }

    static (string, Color) MainChip(JsonElement r)
    {
        var d = r.S("default", "main");
        return r.S("main_state") switch
        {
            "in_sync" => ($"{d} in sync", Good),
            "behind" => ($"{d} behind {r.N("behind")}", Info),
            "ahead" => ($"{d} ahead {r.N("ahead")}", Warn),
            "diverged" => ($"{d} diverged ↑{r.N("ahead")} ↓{r.N("behind")}", Bad),
            "no_remote" => ("no remote", Muted),
            _ => ("no main branch", Muted),
        };
    }

    void RenderStatus()
    {
        foreach (var r in scan.A("repos"))
        {
            var path = r.S("path");
            var body = new StackPanel();
            var head = Row(T(r.S("name"), 14, Text, bold: true));
            head.Children.Add(new TextBlock { Width = 8 });
            if (r.Has("error"))
            {
                head.Children.Add(Chip("error", Bad));
                body.Children.Add(head);
                body.Children.Add(T(r.S("error"), 12, Dim, wrap: true));
                body.Children.Add(T(path, 11, Dim));
                list.Children.Add(Card(body));
                continue;
            }
            var (label, color) = MainChip(r);
            head.Children.Add(Chip(label, color));
            var dirty = r.O("dirty");
            if (dirty.N("total") > 0) head.Children.Add(Chip($"{dirty.N("total")} changed", Warn));
            if (r.N("stashes") > 0) head.Children.Add(Chip($"{r.N("stashes")} stash", Info));
            body.Children.Add(head);

            var fetched = r.D("fetched_age_h");
            var meta = $"{(r.Has("branch") ? "on " + r.S("branch") : "detached HEAD")} · last commit {J.Ago(r.N("last_commit"))} · " +
                       (fetched == null ? "never fetched" : $"fetched {(fetched < 1 ? "<1 h" : $"{fetched:0} h")} ago");
            body.Children.Add(T(meta, 11, Dim));
            foreach (var note in r.EnumerateNotes())
                body.Children.Add(T("• " + note, 12, Br("#F5C86B"), wrap: true));

            var actions = Row();
            actions.Margin = new Thickness(0, 6, 0, 0);
            if (dirty.N("total") > 0)
                actions.Children.Add(Pill(expanded.Contains(path) ? "Hide diff" : "Diff", () =>
                {
                    if (!expanded.Remove(path)) expanded.Add(path);
                    Render();
                    return Task.CompletedTask;
                }));
            bool unpushed = r.A("unpushed_branches").Any() || r.N("stashes") > 0;
            if (dirty.N("total") > 0 || unpushed)
                actions.Children.Add(Pill("Backup", () => Backup(r), tip: "Copy dirty files and bundle unpushed commits/stashes. Changes nothing in the repo."));
            if (r.S("main_state") == "behind")
                actions.Children.Add(Pill("Sync", () => Sync(r, false), primary: true, tip: "Fast-forward only. Never merges or rebases."));
            if (r.S("main_state") == "ahead")
                actions.Children.Add(Pill("Push", () => Sync(r, true), tip: "Push without force."));
            actions.Children.Add(Pill("Open", () => { OpenFolder(path); return Task.CompletedTask; }));
            body.Children.Add(actions);

            if (expanded.Contains(path)) body.Children.Add(FileList(r));
            list.Children.Add(Card(body));
        }
    }

    UIElement FileList(JsonElement r)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
        var dirty = r.O("dirty");
        foreach (var f in dirty.A("files"))
        {
            var kind = f.S("kind");
            var mark = kind switch { "untracked" => "?", "deleted" => "D", "added" => "A", "conflicted" => "!", _ => "M" };
            var row = new DockPanel { Margin = new Thickness(0, 1, 0, 1), Cursor = Cursors.Hand, Background = Brushes.Transparent };
            var stats = new StackPanel { Orientation = Orientation.Horizontal };
            if (f.Has("added")) stats.Children.Add(T($"+{f.N("added")} ", 11, new SolidColorBrush(Good)));
            if (f.Has("removed")) stats.Children.Add(T($"−{f.N("removed")}", 11, new SolidColorBrush(Bad)));
            if (kind == "untracked") stats.Children.Add(T("new", 11, new SolidColorBrush(Info)));
            DockPanel.SetDock(stats, Dock.Right);
            row.Children.Add(stats);
            row.Children.Add(Row(T(mark + "  ", 12, Accent), T(f.S("path"), 12)));
            var filePath = f.S("path");
            var repoPath = r.S("path");
            row.MouseLeftButtonDown += (_, e) => e.Handled = true;
            row.MouseLeftButtonUp += async (_, _) => await ShowDiff(repoPath, filePath);
            panel.Children.Add(row);
        }
        if (dirty.B("truncated")) panel.Children.Add(T("…more files not shown", 11, Dim));
        return panel;
    }

    void RenderSpace()
    {
        var toolbar = Row(
            Pill("Select all Safe", () => { SelectAllSafe(); Render(); return Task.CompletedTask; }),
            Pill("Clear", () => { selected.Clear(); Render(); return Task.CompletedTask; }),
            Pill(CleanLabel(), CleanSelected, primary: true));
        toolbar.Margin = new Thickness(0, 0, 0, 8);
        list.Children.Add(toolbar);

        foreach (var r in scan.A("repos").Where(r => !r.Has("error")))
            AddGroup(r.S("name"), r.S("path"), r.A("items"),
                     $"{J.Human(r.N("worktree_bytes"))} files · {J.Human(r.N("git_bytes"))} .git");
        var caches = scan.A("caches").ToList();
        if (caches.Count > 0) AddGroup("Global package caches", "", caches, "outside repositories");
    }

    void AddGroup(string name, string key, IEnumerable<JsonElement> items, string meta)
    {
        var all = items.ToList();
        var offer = all.Where(i => i.S("status") != "keep").OrderByDescending(i => i.N("bytes")).ToList();
        var kept = all.Count - offer.Count;
        if (offer.Count == 0) return;
        var body = new StackPanel();
        var head = Row(T(name, 14, Text, bold: true));
        head.Children.Add(new TextBlock { Width = 8 });
        head.Children.Add(T(meta, 11, Dim));
        body.Children.Add(head);
        foreach (var it in offer)
        {
            var id = it.S("id");
            var safe = it.S("status") == "safe";
            var box = new CheckBox
            {
                IsChecked = selected.TryGetValue(key, out var set) && set.Contains(id),
                VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(0, 3, 8, 0),
            };
            box.Checked += (_, _) => { Sel(key).Add(id); UpdateCleanLabel(); };
            box.Unchecked += (_, _) => { Sel(key).Remove(id); UpdateCleanLabel(); };
            var text = new StackPanel();
            var line = Row(Chip(safe ? "Safe" : "Review", safe ? Good : Warn), T(KindLabel(it), 12, Dim), T(" " + it.S("target"), 12));
            text.Children.Add(line);
            text.Children.Add(T(it.S("reason"), 11, Dim, wrap: true));
            var size = T(it.N("bytes") > 0 ? J.Human(it.N("bytes")) : "", 12, Text, bold: true);
            var row = new DockPanel { Margin = new Thickness(0, 6, 0, 0) };
            DockPanel.SetDock(box, Dock.Left);
            DockPanel.SetDock(size, Dock.Right);
            row.Children.Add(box);
            row.Children.Add(size);
            row.Children.Add(text);
            body.Children.Add(row);
        }
        if (kept > 0) body.Children.Add(T($"{kept} item(s) kept automatically (default branch, unpushed or uncommitted work, tracked files)", 11, Dim, wrap: true));
        list.Children.Add(Card(body));
    }

    static string KindLabel(JsonElement it) => it.S("kind") switch
    {
        "branch" => "branch",
        "worktree" => "worktree",
        "gc" => "git",
        "cache" => it.S("label"),
        _ => it.S("label", "folder"),
    };

    HashSet<string> Sel(string key) => selected.TryGetValue(key, out var s) ? s : selected[key] = new HashSet<string>();

    void SelectAllSafe()
    {
        selected.Clear();
        foreach (var r in scan.A("repos").Where(r => !r.Has("error")))
            foreach (var it in r.A("items").Where(i => i.S("status") == "safe"))
                Sel(r.S("path")).Add(it.S("id"));
        foreach (var it in scan.A("caches").Where(i => i.S("status") == "safe"))
            Sel("").Add(it.S("id"));
    }

    IEnumerable<(string Key, JsonElement Item)> SelectedItems()
    {
        foreach (var r in scan.A("repos").Where(r => !r.Has("error")))
            foreach (var it in r.A("items"))
                if (selected.TryGetValue(r.S("path"), out var s) && s.Contains(it.S("id"))) yield return (r.S("path"), it);
        foreach (var it in scan.A("caches"))
            if (selected.TryGetValue("", out var s) && s.Contains(it.S("id"))) yield return ("", it);
    }

    string CleanLabel()
    {
        var items = hasScan ? SelectedItems().ToList() : new List<(string Key, JsonElement Item)>();
        return items.Count == 0 ? "Clean selected" : $"Clean {items.Count} · {J.Human(items.Sum(i => i.Item.N("bytes")))}";
    }

    void UpdateCleanLabel()
    {
        if (list.Children.Count > 0 && list.Children[0] is StackPanel bar && bar.Children.Count >= 3 &&
            bar.Children[2] is Border pill && pill.Child is TextBlock tb)
            tb.Text = CleanLabel();
    }

    // ------------------------------------------------------------ actions

    async Task CleanSelected()
    {
        if (!hasScan) return;
        var items = SelectedItems().ToList();
        if (items.Count == 0) { Say("Select items first, or use “Select all Safe”."); return; }
        var bytes = items.Sum(i => i.Item.N("bytes"));
        var review = items.Count(i => i.Item.S("status") == "review");
        var msg = $"Remove {items.Count} item(s), about {J.Human(bytes)}?\n\n" +
                  "• Branches are saved as git bundles in your backup folder first.\n" +
                  "• Folders go to the Recycle Bin.\n" +
                  "• Every item is re-checked right before removal; anything that changed is skipped.";
        if (review > 0) msg += $"\n\n{review} item(s) are marked REVIEW. They will be removed only because you selected them.";
        if (MessageBox.Show(msg, "Repo Pulse — confirm cleanup", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK) return;

        long freed = 0;
        var failures = new List<string>();
        var recycleRefused = new List<(string Key, string Id, string Target)>();
        foreach (var group in items.GroupBy(i => i.Key))
        {
            var args = new List<string> { "clean", "--ids", string.Join(",", group.Select(g => g.Item.S("id"))) };
            if (group.Key != "") { args.Add("--repo"); args.Add(group.Key); }
            if (group.Any(g => g.Item.S("status") == "review")) args.Add("--allow-review");
            var res = await Act($"Cleaning {Path.GetFileName(group.Key.TrimEnd('\\', '/'))}…", WithDest(args.ToArray()));
            if (res is not { } result) return;
            if (!result.B("ok")) { failures.Add(result.S("error")); continue; }
            freed += result.N("freed_bytes");
            foreach (var x in result.A("results").Where(x => !x.B("done")))
            {
                failures.Add($"{x.S("target")}: {x.S("reason")}");
                var item = group.FirstOrDefault(g => g.Item.S("id") == x.S("id")).Item;
                if (x.S("reason").Contains("Recycle Bin refused") && item.S("status") == "safe")
                    recycleRefused.Add((group.Key, x.S("id"), x.S("target")));
            }
        }

        if (recycleRefused.Count > 0 && MessageBox.Show(
                $"{recycleRefused.Count} rebuildable folder(s) could not go to the Recycle Bin (usually paths that are too long).\n\n" +
                "Delete them permanently? They are Safe items that can be rebuilt (npm install, pip install, build).",
                "Repo Pulse", MessageBoxButton.YesNo, MessageBoxImage.Warning) == MessageBoxResult.Yes)
        {
            foreach (var group in recycleRefused.GroupBy(r => r.Key))
            {
                var args = new List<string> { "clean", "--permanent", "--ids", string.Join(",", group.Select(g => g.Id)) };
                if (group.Key != "") { args.Add("--repo"); args.Add(group.Key); }
                if (await Act("Deleting rebuildable folders…", WithDest(args.ToArray())) is { } res && res.B("ok"))
                {
                    freed += res.N("freed_bytes");
                    foreach (var x in res.A("results").Where(x => x.B("done")))
                        failures.RemoveAll(f => f.StartsWith(x.S("target") + ":"));
                }
            }
        }

        selected.Clear();
        await Scan(false);
        Say($"Freed about {J.Human(freed)}." + (failures.Count > 0 ? $" {failures.Count} skipped: " + string.Join(" | ", failures.Take(3)) : ""));
    }

    async Task Backup(JsonElement r)
    {
        var res = await Act($"Backing up {r.S("name")}…", WithDest("backup", "--repo", r.S("path")));
        if (res is not { } result) return;
        if (!result.B("ok")) { MessageBox.Show(result.S("error"), "Backup not made", MessageBoxButton.OK, MessageBoxImage.Warning); Say("Backup not made."); return; }
        var dest = result.S("backup");
        var bundle = result.O("bundle");
        var detail = $"{result.N("files")} file(s), {J.Human(result.N("bytes"))}" +
                     (bundle.ValueKind == JsonValueKind.Object ? (bundle.B("ok") ? " + unpushed commits/stashes bundle" : " — bundle FAILED: " + bundle.S("error")) : "");
        Say($"Backed up {r.S("name")}: {detail}");
        if (MessageBox.Show($"Backup saved:\n{dest}\n\n{detail}\n\nOpen the folder?", "Repo Pulse", MessageBoxButton.YesNo, MessageBoxImage.Information) == MessageBoxResult.Yes)
            OpenFolder(dest);
    }

    async Task Sync(JsonElement r, bool push)
    {
        var d = r.S("default", "main");
        var text = push
            ? $"Push {r.N("ahead")} commit(s) from {d} to {r.S("remote")}/{d}?\n\nNo force-push. Git refuses if the remote moved."
            : $"Fast-forward {d} by {r.N("behind")} commit(s) to match {r.S("remote")}/{d}?\n\nFast-forward only: never merges or rebases, and stops if your changes would be overwritten.";
        if (MessageBox.Show(text, "Repo Pulse", MessageBoxButton.OKCancel, MessageBoxImage.Question) != MessageBoxResult.OK) return;
        var args = push ? new[] { "sync", "--repo", r.S("path"), "--push" } : new[] { "sync", "--repo", r.S("path") };
        if (await Act($"Syncing {r.S("name")}…", args) is not { } result) return;
        if (!result.B("ok")) { MessageBox.Show(result.S("error"), "Sync stopped safely", MessageBoxButton.OK, MessageBoxImage.Information); }
        await Scan(false);
        Say(result.B("ok") ? string.Join(" · ", result.A("steps").Select(s => s.GetString())) : "Sync stopped: " + result.S("error"));
    }

    async Task ShowDiff(string repo, string file)
    {
        if (await Act($"Loading diff for {file}…", "diff", "--repo", repo, "--path", file) is not { } result) return;
        Say("");
        new DiffWindow(file, result.S("text"), result.B("truncated")) { Owner = this }.Show();
    }

    async Task OpenSettings()
    {
        var dlg = new SettingsWindow(settings) { Owner = this };
        if (dlg.ShowDialog() == true)
        {
            settings.Save();
            await Scan(false);
        }
    }

    static void OpenFolder(string path)
    {
        try { Process.Start(new ProcessStartInfo("explorer.exe", $"\"{path}\"") { UseShellExecute = true }); } catch { }
    }
}

static class ScanExtensions
{
    public static IEnumerable<string> EnumerateNotes(this JsonElement repo) =>
        repo.A("attention").Select(a => a.GetString() ?? "").Where(s => s.Length > 0);
}
