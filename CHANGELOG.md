# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); releases are on
[GitHub](https://github.com/kumarnabh/repo-pulse/releases).

## 0.4.0 — 2026-10-11

### Platform
- The widget now runs on **.NET 10** (LTS, supported until November 2028). .NET 8 support ends on November 10, 2026.
- The small download is now `…-needs-dotnet10.zip` and needs the .NET 10 Desktop Runtime. The full download still needs only Python.
- Building from source needs the .NET 10 SDK; `Build.cmd` says so if it is missing.

## 0.3.0 — 2026-10-11

First public release, with ready-to-run downloads.

### Safety fixes
- **Worktrees holding ignored files are no longer Safe.** `git worktree remove` deletes ignored files without asking, so a clean, pushed agent worktree with a `.env`, local config or data file was offered for removal. It is now Review, and the reason names the files. Rebuildable folders (`node_modules`, `.venv`, …) still count as Safe.
- **Backup saves every stash.** Only the newest stash travelled in the bundle; each one is now also written as `stash-N.patch`.
- **Backup survives locked files.** A file another program holds open no longer aborts the backup halfway; it is listed as not copied.

### Bug fixes
- **Fetch + scan** no longer marks every local-only repository (no remote) as "Fetch failed".
- Renamed files inside folders now show their +/− line counts.
- A repository row that fails to scan keeps its worktree/repo kind, so totals stay right.
- Folders that tool history remembers but that were since deleted no longer resolve to an unrelated parent repository.
- The widget no longer hangs at start if a Python interpreter stops responding while being probed.
- A cleanup interrupted part-way now still rescans, so the list never shows removed items.
- Permanent delete uses `shutil.rmtree(onexc=…)` on Python 3.12+ (the old `onerror` is deprecated).
- Tests no longer read the host's VS Code history from the CLI child process.

### Faster
- Full scans are about twice as fast with many worktrees: each linked worktree is walked once, not twice.
- Repos with hundreds of branches need one git call to find unpushed work instead of one per branch.

### New
- **Skip folders**: ⚙ settings and `scan --exclude "A;B"`. Skipped folders are never walked or shown.
- **More discovery**: VS Code / Cursor / Windsurf recently opened folders (from `state.vscdb`, read-only) and JetBrains IDE recent projects.
- **`scan --text`**: a readable terminal report with item ids for `clean`. The core runs on Windows, Linux and macOS.
- Widget: F5 scans, Shift+F5 fetches and scans; the window reopens where you left it; version shown in ⚙.
- **Releases**: tagged versions build a self-contained Windows download (needs only Python) and a small one for machines with .NET 8, with SHA-256 checksums.

### Quality
- 31 tests. CI now runs the core on Windows, Linux and macOS with Python 3.10 and 3.13.

## 0.2.0 — 2026-10-11

### Finds every git folder
- With no folders configured, scans **every fixed drive** instead of only the user folder.
- Adds folders known to **Claude Code**, **Codex**, **VS Code**, **Cursor** and **Windsurf**, even outside the scanned folders.
- **Linked worktrees** (agent worktrees included) get their own rows, wherever they live, showing their branch against its upstream and their own rebuildable folders.
- Walk depth raised from 5 to 6; more system and tool folders skipped.
- Fetch runs on every main repository before scanning, so worktree rows read fresh refs.
- New `--no-hints` scan option.

### Compact layout
- Repositories needing attention are one-line rows with a status dot and compact chips; click to expand.
- Healthy repositories are a tile grid. Worktrees are nested under their repository.
- The window fits its height to the content with no visible scrollbar. Resizing by hand turns this off; double-click the header to turn it back on.
- Space tab items are one line each, with the reason in a tooltip.
- The summary's tooltip shows which folders were searched and how each repository was found.

### Quality
- 25 tests (new: worktrees outside roots, submodules ignored, tool-history discovery, worktree rows, branch behind upstream). Tests no longer read the host machine's tool history.

## 0.1.0 — 2026-10-11

First release.

### Status
- Discovers git repositories under configurable folders (skips AppData, caches and dependency folders).
- Local default branch vs `origin/<default>`: in sync, behind, ahead, diverged, no remote.
- Dirty files with staged/modified/untracked/conflicted counts, per-file +/− lines and a click-to-view diff.
- Stashes, unpushed branches, last commit and last fetch age, with an attention summary per repo.
- Optional `git fetch --prune` per repo (updates remote-tracking refs only).

### Actions
- **Backup**: copies dirty files, writes `changes.patch` and `staged.patch`, bundles unpushed commits and stashes, with a 2 GB default limit.
- **Sync**: fast-forward only, including when the default branch is not checked out. Optional push without force.
- **Cleanup**: branches, worktrees, rebuildable folders, git garbage and global package caches, classified Safe / Review / Keep and re-verified immediately before removal. Branches are bundled first; folders go to the Recycle Bin, with a permanent-delete fallback offered only for Safe rebuildable folders.

### Widget
- .NET 8 WPF glass window in the Prism palette with Status and Space tabs, settings and a diff viewer.

### Quality
- 21 fixture tests with real git operations against a local bare remote.
- CI runs the core tests on Windows and Linux and builds the widget on Windows.
