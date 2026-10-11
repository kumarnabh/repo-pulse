// Copyright (c) 2026 Nabh Kumar. MIT License.
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using static RepoPulse.Ui;

namespace RepoPulse;

public sealed class MainWindow : Window
{
    readonly Settings settings = Settings.Load();
    readonly StackPanel list = new();
    readonly ScrollViewer scroller;
    readonly TextBlock summary = T("Scanning…", 12, Dim);
    readonly TextBlock footer = T("", 11, Dim, wrap: true);
    readonly Border tabStatus, tabSpace;
    readonly HashSet<string> expanded = new();   // repo rows showing details
    readonly HashSet<string> diffOpen = new();   // repo rows showing changed files
    // repo path ("" for global caches) -> selected item ids
    readonly Dictionary<string, HashSet<string>> selected = new();
    JsonElement scan;
    bool hasScan, busy, autoFit = true;
    double fitTarget;
    string tab = "status";

    static readonly TimeSpan ScanTimeout = TimeSpan.FromMinutes(15);

    public MainWindow()
    {
        Title = "Repo Pulse";
        Width = 520; Height = 560; MinWidth = 400; MinHeight = 260;
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
        var header = new DockPanel { LastChildFill = false, Background = Brushes.Transparent,
                                     ToolTip = "Drag to move · double-click to fit the window to its content" };
        DockPanel.SetDock(actions, Dock.Right);
        header.Children.Add(actions);
        header.Children.Add(title);
        header.MouseLeftButtonDown += (_, e) =>
        {
            if (e.ClickCount == 2) { autoFit = true; ScheduleFit(); return; }
            if (e.ButtonState == MouseButtonState.Pressed) DragMove();
        };

        tabStatus = Tab("Status", "status");
        tabSpace = Tab("Space", "space");
        var tabs = Row(tabStatus, tabSpace);
        tabs.Margin = new Thickness(0, 8, 0, 6);

        // Scrolling still works with the wheel when content exceeds the screen; the bar itself stays hidden.
        scroller = new ScrollViewer { Content = list, VerticalScrollBarVisibility = ScrollBarVisibility.Hidden,
                                      HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
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
        RestorePlacement();
        Closing += (_, _) => SavePlacement();
        KeyDown += async (_, e) =>
        {
            if (e.Key != Key.F5) return;
            e.Handled = true;
            await Scan(Keyboard.Modifiers.HasFlag(ModifierKeys.Shift));
        };
        Loaded += async (_, _) => await Scan(false);
        SizeChanged += (_, e) =>
        {
            // A height we did not set means the user resized by hand: stop auto-fitting until a header double-click.
            if (fitTarget > 0 && e.HeightChanged && Math.Abs(e.NewSize.Height - fitTarget) > 2) autoFit = false;
        };
    }

    Border Tab(string text, string key)
    {
        var b = new Border { CornerRadius = new CornerRadius(10), Padding = new Thickness(14, 3, 14, 4), Margin = new Thickness(0, 0, 6, 0), Cursor = Cursors.Hand,
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

    /// <summary>Reopen where the window was last closed, if that spot is still on a screen.</summary>
    void RestorePlacement()
    {
        if (settings.Width is { } w && w >= MinWidth) Width = Math.Min(w, SystemParameters.VirtualScreenWidth);
        if (settings.Left is not { } left || settings.Top is not { } top) return;
        var screen = new Rect(SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop,
                              SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight);
        if (!screen.Contains(new Point(left + 40, top + 20))) return;  // monitor unplugged since: stay centered
        WindowStartupLocation = WindowStartupLocation.Manual;
        Left = left;
        Top = top;
    }

    void SavePlacement()
    {
        if (WindowState != WindowState.Normal) return;
        settings.Left = Left;
        settings.Top = Top;
        settings.Width = ActualWidth;
        try { settings.Save(); } catch { }  // a read-only install folder must not block closing
    }

    // ------------------------------------------------------------ fit to content

    void ScheduleFit() => Dispatcher.BeginInvoke(new Action(FitHeight), DispatcherPriority.Loaded);

    void FitHeight()
    {
        if (!autoFit || WindowState != WindowState.Normal || scroller.ActualHeight <= 0) return;
        list.Measure(new Size(Math.Max(100, scroller.ActualWidth), double.PositiveInfinity));
        var chrome = ActualHeight - scroller.ActualHeight;
        var area = SystemParameters.WorkArea;
        var target = Math.Min(Math.Max(MinHeight, chrome + list.DesiredSize.Height + 4), area.Height - 16);
        fitTarget = target;
        if (Math.Abs(target - ActualHeight) < 2) return;
        Height = target;
        if (Top + target > area.Bottom) Top = Math.Max(area.Top, area.Bottom - target);
    }

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
            if (!string.IsNullOrWhiteSpace(settings.Exclude)) { args.Add("--exclude"); args.Add(settings.Exclude); }
            if (settings.IncludeCaches) args.Add("--caches");
            if (fetch) args.Add("--fetch");
            var result = await Core.Run(ScanTimeout, args.ToArray());
            if (!result.B("ok")) { Say("Scan failed: " + result.S("error")); return; }
            scan = result;
            hasScan = true;
            selected.Clear();
            Render();
            Say($"Scanned {DateTime.Now:HH:mm} in {result.N("scan_ms") / 1000.0:0.0}s. " +
                (fetch ? "Only remote-tracking refs were updated." : "Nothing was changed."));
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
        var wt = totals.N("worktrees");
        summary.Text = $"{totals.N("repos")} repos" + (wt > 0 ? $" · {wt} worktrees" : "") +
                       $" · {totals.N("needs_attention")} need attention · {J.Human(totals.N("safe_bytes"))} safe to reclaim";
        var sources = scan.O("sources");
        var found = sources.ValueKind == JsonValueKind.Object
            ? string.Join(", ", sources.EnumerateObject().Select(p => $"{p.Name} {p.Value.GetDouble():0}")) : "";
        summary.ToolTip = "Searched: " + string.Join("; ", scan.A("roots").Select(r => r.GetString())) +
                          (found.Length > 0 ? "\nFound via: " + found : "");
        if (tab == "status") RenderStatus(); else RenderSpace();
        if (list.Children.Count == 0)
            list.Children.Add(T("No git repositories found. Add folders in ⚙ settings.", 13, Dim, wrap: true));
        ScheduleFit();
    }

    static string Norm(string p) => p.Replace('/', '\\').TrimEnd('\\').ToLowerInvariant();
    static bool IsWorktree(JsonElement r) => r.S("kind") == "worktree";
    static bool NeedsAttention(JsonElement r) => r.Has("error") || r.A("attention").Any();

    /// <summary>Main repos, each followed by its linked worktrees.</summary>
    List<JsonElement> Ordered()
    {
        var repos = scan.A("repos").ToList();
        var children = repos.Where(IsWorktree).GroupBy(r => Norm(r.S("parent"))).ToDictionary(g => g.Key, g => g.ToList());
        var ordered = new List<JsonElement>();
        foreach (var m in repos.Where(r => !IsWorktree(r)).OrderBy(r => r.S("name"), StringComparer.OrdinalIgnoreCase))
        {
            ordered.Add(m);
            if (children.Remove(Norm(m.S("path")), out var w)) ordered.AddRange(w.OrderBy(x => x.S("name"), StringComparer.OrdinalIgnoreCase));
        }
        foreach (var orphans in children.Values) ordered.AddRange(orphans);
        return ordered;
    }

    static Color DotColor(JsonElement r)
    {
        if (r.Has("error") || r.Has("fetch_error") || r.S("main_state") == "diverged" || r.O("dirty").N("conflicted") > 0) return Bad;
        if (r.O("dirty").N("total") > 0 || r.S("main_state") == "ahead" || r.N("branch_ahead") > 0 || r.A("unpushed_branches").Any()) return Warn;
        if (r.S("main_state") == "behind" || r.N("branch_behind") > 0 || r.N("stashes") > 0) return Info;
        return Good;
    }

    static System.Windows.Shapes.Ellipse Dot(Color c, double left = 0) => new()
    {
        Width = 8, Height = 8, Fill = new SolidColorBrush(c), Margin = new Thickness(left, 0, 8, 0), VerticalAlignment = VerticalAlignment.Center,
    };

    static TextBlock Section(string text) { var t = T(text.ToUpperInvariant(), 10, Dim, bold: true); t.Margin = new Thickness(2, 4, 0, 4); return t; }

    void Toggle(HashSet<string> set, string key) { if (!set.Remove(key)) set.Add(key); Render(); }

    void RenderStatus()
    {
        var ordered = Ordered();
        var needs = ordered.Where(NeedsAttention).ToList();
        var healthy = ordered.Where(r => !NeedsAttention(r)).ToList();
        if (needs.Count > 0)
        {
            list.Children.Add(Section($"Needs attention · {needs.Count}"));
            var panel = new StackPanel();
            foreach (var r in needs) panel.Children.Add(RepoRow(r));
            list.Children.Add(Card(panel));
        }
        if (healthy.Count > 0)
        {
            list.Children.Add(Section($"All good · {healthy.Count}"));
            var wrap = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            foreach (var r in healthy) wrap.Children.Add(Tile(r));
            list.Children.Add(wrap);
            foreach (var r in healthy.Where(r => expanded.Contains(r.S("path"))))
            {
                var panel = new StackPanel();
                panel.Children.Add(RepoRow(r));
                list.Children.Add(Card(panel));
            }
        }
    }

    UIElement RepoRow(JsonElement r)
    {
        var path = r.S("path");
        var wt = IsWorktree(r);
        var name = Row(Dot(DotColor(r), wt ? 14 : 0), T((wt ? "⎇ " : "") + r.S("name"), 13, Text, bold: !wt));
        var branch = r.S("branch");
        if (wt) name.Children.Add(T("  " + r.S("parent_name") + (branch.Length > 0 ? " · " + branch : ""), 11, Dim));
        else if (branch.Length > 0 && branch != r.S("default")) name.Children.Add(T("  " + branch, 11, Dim));
        name.Margin = new Thickness(0);

        var chips = StateChips(r);
        var line = new DockPanel { Background = Brushes.Transparent, Cursor = Cursors.Hand, MinHeight = 24, Margin = new Thickness(0, 1, 0, 1) };
        var notes = r.Has("error") ? new List<string> { r.S("error") } : r.EnumerateNotes().ToList();
        if (notes.Count > 0) line.ToolTip = string.Join("\n", notes);
        DockPanel.SetDock(chips, Dock.Right);
        line.Children.Add(chips);
        line.Children.Add(name);
        line.MouseLeftButtonDown += (_, e) => e.Handled = true;
        line.MouseLeftButtonUp += (_, _) => Toggle(expanded, path);

        var box = new StackPanel();
        box.Children.Add(line);
        if (expanded.Contains(path))
        {
            var details = Details(r);
            details.Margin = new Thickness(wt ? 30 : 16, 2, 0, 8);
            box.Children.Add(details);
        }
        return box;
    }

    static StackPanel StateChips(JsonElement r)
    {
        var chips = Row();
        chips.Margin = new Thickness(0);
        if (r.Has("error")) { chips.Children.Add(Chip("error", Bad)); return chips; }
        if (r.Has("fetch_error")) chips.Children.Add(Chip("fetch failed", Bad));
        var d = r.S("default", "main");
        switch (r.S("main_state"))
        {
            case "behind": chips.Children.Add(Chip($"{d} ↓{r.N("behind")}", Info)); break;
            case "ahead": chips.Children.Add(Chip($"{d} ↑{r.N("ahead")}", Warn)); break;
            case "diverged": chips.Children.Add(Chip($"{d} ↑{r.N("ahead")} ↓{r.N("behind")}", Bad)); break;
            case "no_remote": chips.Children.Add(Chip("local only", Muted)); break;
        }
        long ba = r.N("branch_ahead"), bb = r.N("branch_behind");
        var onOther = r.S("branch").Length > 0 && (IsWorktree(r) || r.S("branch") != d);
        if (onOther && (ba > 0 || bb > 0))
            chips.Children.Add(Chip((ba > 0 ? $"↑{ba}" : "") + (ba > 0 && bb > 0 ? " " : "") + (bb > 0 ? $"↓{bb}" : ""), ba > 0 && bb > 0 ? Bad : ba > 0 ? Warn : Info));
        var dirty = r.O("dirty");
        if (dirty.N("conflicted") > 0) chips.Children.Add(Chip($"{dirty.N("conflicted")} conflict", Bad));
        else if (dirty.N("total") > 0) chips.Children.Add(Chip($"✎ {dirty.N("total")}", Warn));
        var otherUnpushed = r.A("unpushed_branches").Count(b => b.S("name") != r.S("branch") && b.S("name") != d);
        if (otherUnpushed > 0) chips.Children.Add(Chip($"⎇ {otherUnpushed} unpushed", Warn));
        if (r.N("stashes") > 0) chips.Children.Add(Chip($"≡ {r.N("stashes")}", Info));
        if (!r.B("fresh") && r.Has("remote") && !IsWorktree(r)) chips.Children.Add(Chip("stale", Muted));
        return chips;
    }

    Border Tile(JsonElement r)
    {
        var path = r.S("path");
        var wt = IsWorktree(r);
        var open = expanded.Contains(path);
        var tile = new Border
        {
            Background = open ? Br("#4089E6D5") : Ui.Panel, CornerRadius = new CornerRadius(9), Padding = new Thickness(8, 3, 10, 4),
            Margin = new Thickness(0, 0, 6, 6), Cursor = Cursors.Hand, BorderBrush = Line, BorderThickness = new Thickness(1),
            Child = Row(Dot(DotColor(r)), T((wt ? "⎇ " : "") + r.S("name"), 12)),
            ToolTip = $"{path}\n{(r.Has("branch") ? "on " + r.S("branch") : "detached HEAD")} · last commit {J.Ago(r.N("last_commit"))}" +
                      (wt ? $"\nworktree of {r.S("parent_name")}" : ""),
        };
        ((StackPanel)tile.Child).Margin = new Thickness(0);
        tile.MouseLeftButtonDown += (_, e) => e.Handled = true;
        tile.MouseLeftButtonUp += (_, _) => Toggle(expanded, path);
        return tile;
    }

    StackPanel Details(JsonElement r)
    {
        var path = r.S("path");
        var body = new StackPanel();
        if (r.Has("error"))
        {
            body.Children.Add(T(r.S("error"), 12, Dim, wrap: true));
            body.Children.Add(T(path, 11, Dim));
            return body;
        }
        var fetched = r.D("fetched_age_h");
        var meta = $"{(r.Has("branch") ? "on " + r.S("branch") : "detached HEAD")} · last commit {J.Ago(r.N("last_commit"))} · " +
                   (fetched == null ? "never fetched" : $"fetched {(fetched < 1 ? "<1 h" : $"{fetched:0} h")} ago");
        body.Children.Add(T(meta, 11, Dim));
        body.Children.Add(T(path, 11, Dim));
        foreach (var note in r.EnumerateNotes())
            body.Children.Add(T("• " + note, 12, Br("#F5C86B"), wrap: true));

        var dirty = r.O("dirty");
        var actions = Row();
        actions.Margin = new Thickness(0, 6, 0, 0);
        if (dirty.N("total") > 0)
            actions.Children.Add(Pill(diffOpen.Contains(path) ? "Hide files" : "Files", () => { Toggle(diffOpen, path); return Task.CompletedTask; }));
        bool unpushed = r.A("unpushed_branches").Any() || r.N("stashes") > 0 || r.N("branch_ahead") > 0;
        if (dirty.N("total") > 0 || unpushed)
            actions.Children.Add(Pill("Backup", () => Backup(r), tip: "Copy dirty files and bundle unpushed commits/stashes. Changes nothing in the repo."));
        if (r.S("main_state") == "behind")
            actions.Children.Add(Pill("Sync", () => Sync(r, false), primary: true, tip: "Fast-forward only. Never merges or rebases."));
        if (r.S("main_state") == "ahead")
            actions.Children.Add(Pill("Push", () => Sync(r, true), tip: "Push without force."));
        actions.Children.Add(Pill("Open", () => { OpenFolder(path); return Task.CompletedTask; }));
        body.Children.Add(actions);
        if (diffOpen.Contains(path)) body.Children.Add(FileList(r));
        return body;
    }

    UIElement FileList(JsonElement r)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
        var dirty = r.O("dirty");
        foreach (var f in dirty.A("files"))
        {
            var kind = f.S("kind");
            var mark = kind switch { "untracked" => "?", "deleted" => "D", "added" => "A", "conflicted" => "!", _ => "M" };
            var row = new DockPanel { Margin = new Thickness(0, 1, 0, 1), Cursor = Cursors.Hand, Background = Brushes.Transparent, ToolTip = "Show diff" };
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

        int groups = 0;
        foreach (var r in Ordered().Where(r => !r.Has("error")))
        {
            var meta = IsWorktree(r) ? $"worktree of {r.S("parent_name")}" : $"{J.Human(r.N("worktree_bytes"))} files · {J.Human(r.N("git_bytes"))} .git";
            if (AddGroup((IsWorktree(r) ? "⎇ " : "") + r.S("name"), r.S("path"), r.A("items"), meta)) groups++;
        }
        var caches = scan.A("caches").ToList();
        if (caches.Count > 0 && AddGroup("Global package caches", "", caches, "outside repositories")) groups++;
        if (groups == 0) list.Children.Add(T("Nothing to reclaim right now.", 13, Dim));
    }

    bool AddGroup(string name, string key, IEnumerable<JsonElement> items, string meta)
    {
        var all = items.ToList();
        var offer = all.Where(i => i.S("status") != "keep").OrderByDescending(i => i.N("bytes")).ToList();
        if (offer.Count == 0) return false;
        var kept = all.Count - offer.Count;
        var body = new StackPanel();
        var head = Row(T(name, 13, Text, bold: true), T("  " + meta + (kept > 0 ? $" · {kept} kept" : ""), 11, Dim));
        head.Margin = new Thickness(0, 0, 0, 2);
        if (kept > 0) head.ToolTip = "Kept automatically: default branch, unpushed or uncommitted work, tracked files";
        body.Children.Add(head);
        foreach (var it in offer)
        {
            var id = it.S("id");
            var safe = it.S("status") == "safe";
            var box = new CheckBox
            {
                IsChecked = selected.TryGetValue(key, out var set) && set.Contains(id),
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0),
            };
            box.Checked += (_, _) => { Sel(key).Add(id); UpdateCleanLabel(); };
            box.Unchecked += (_, _) => { Sel(key).Remove(id); UpdateCleanLabel(); };
            var size = T(it.N("bytes") > 0 ? J.Human(it.N("bytes")) : "", 12, Text, bold: true);
            var text = Row(Chip(safe ? "Safe" : "Review", safe ? Good : Warn), T(KindLabel(it) + " ", 12, Dim), T(it.S("target"), 12));
            text.Margin = new Thickness(0);
            var row = new DockPanel { Margin = new Thickness(0, 1, 0, 1), ToolTip = it.S("reason"), Background = Brushes.Transparent };
            DockPanel.SetDock(box, Dock.Left);
            DockPanel.SetDock(size, Dock.Right);
            row.Children.Add(box);
            row.Children.Add(size);
            row.Children.Add(text);
            body.Children.Add(row);
        }
        list.Children.Add(Card(body));
        return true;
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
            if (res is not { } result) { failures.Add($"{group.Key}: core did not finish"); break; }  // still rescan below
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
        var failed = result.A("failed").ToList();
        var detail = $"{result.N("files")} file(s), {J.Human(result.N("bytes"))}" +
                     (bundle.ValueKind == JsonValueKind.Object ? (bundle.B("ok") ? " + unpushed commits/stashes bundle" : " — bundle FAILED: " + bundle.S("error")) : "") +
                     (result.N("stash_patches") > 0 ? $" + {result.N("stash_patches")} stash patch(es)" : "") +
                     (failed.Count > 0 ? $"\n\n{failed.Count} file(s) could not be copied (in use?): " +
                                         string.Join(", ", failed.Take(3).Select(f => f.S("path"))) : "");
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
