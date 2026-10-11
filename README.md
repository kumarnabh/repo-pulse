# Repo Pulse

**Every local repo, at a glance — and the space you can safely take back.**
A small Windows glass widget for developers with too many repositories. Sibling of [Prism](https://github.com/kumarnabh/prism-widget).

![Checks](https://github.com/kumarnabh/repo-pulse/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-88dfd0)
![Platform: Windows x64](https://img.shields.io/badge/platform-Windows%20x64-a9d2ff)

[Specification](SPEC.md) · [Testing](TESTING.md) · [Troubleshooting](TROUBLESHOOTING.md) · [Changelog](CHANGELOG.md)

> **Status: 0.1, early.** The Python core is covered by automated tests on Windows and Linux.
> The widget is new: please report anything odd with the footer message and steps.

- **Status**: does local `main` agree with `origin/main`? Ahead, behind, diverged, dirty files with +/− counts and a click-to-view diff, stashes, unpushed branches.
- **Backup**: one click copies dirty files and bundles unpushed commits and stashes. The repo itself is not touched.
- **Sync**: fast-forward only. Push without force. Never merges, rebases or force-pushes.
- **Space**: merged or fully pushed branches, stale worktrees (including AI-agent worktrees), `node_modules`, `.venv`, build caches, git garbage and global package caches, each marked **Safe** or **Review** with its size.
- **Finds everything**: every fixed drive, plus folders known to Claude Code, Codex, VS Code, Cursor and Windsurf, plus every linked worktree (⎇) wherever it lives.
- **Compact**: problems as one-line rows, healthy repos as tiles; the window fits its content with no scrollbar.

## Start

1. Install **Python 3.10+** (python.org, with the launcher) and the **.NET 8 SDK**.
2. Double-click **Build.cmd**, then **Launch Repo Pulse.cmd**.
3. Optional: in **⚙**, limit scanning to your code folders (e.g. `D:\Code;C:\Users\you\source\repos`) for faster scans.
   Leave it empty to scan every fixed drive.

The first scan is read-only. Use **Fetch + scan** for accurate ahead/behind numbers.
Hover the summary line to see which folders were searched and how each repo was found.

## What "Safe" means

| Item | Safe only when |
|---|---|
| Branch | Not the default branch, not checked out anywhere, every commit is on a remote, and the last fetch is under 24 h old |
| Worktree | Folder already gone (stale metadata), or clean, unlocked and fully pushed |
| Folder | Known rebuildable folder, git-ignored, contains no tracked files, rebuild manifest present |
| git gc | Over 50 MB of loose or garbage objects (normal prune window) |

Everything else is **Review** (needs your explicit selection) or **kept** and never offered.
Every item is re-checked immediately before removal. Branches are saved as git bundles first;
folders go to the Recycle Bin. Restore a branch with:

```
git fetch "<backup>\branch-NAME.bundle" NAME:NAME
```

## Command line

The core works without the widget:

```
py -3 repopulse.py scan --fetch                 # every fixed drive + tool history + worktrees
py -3 repopulse.py scan --roots "D:\Code" --no-hints
py -3 repopulse.py backup --repo "D:\Code\app"
py -3 repopulse.py sync --repo "D:\Code\app" [--push]
py -3 repopulse.py clean --repo "D:\Code\app" --ids ID1,ID2
```

## Privacy

Local only. No telemetry, accounts or network calls, except `git fetch` and `git push`
when you ask for them. Settings live in `data\settings.json`.

## Project layout

| Path | Purpose |
|---|---|
| `repopulse.py` | Core: discovery, scan, classification, backup, sync, cleanup. Standard library only. |
| `source/` | .NET 8 WPF widget (code-only, no XAML) that calls the core and renders its JSON. |
| `tests/` | Fixture tests with real git repositories. |
| `Build.cmd` / `Build.ps1` | Builds `RepoPulse.exe` into this folder. |

## Tests

```
py -3 -m unittest discover -s tests -v
```

25 fixture tests run real git operations against a local bare remote, covering unpushed
commits, stale fetches, squash-merged branches, dirty or locked worktrees, tracked files
inside junk folders, and changes made between scan and cleanup. See [TESTING.md](TESTING.md)
for the manual Windows checklist.

## Not in scope

Merging, rebasing, conflict resolution, force-push, scheduled or automatic cleanup, and
anything needing administrator rights. Repo Pulse shows the problem; you resolve it.

MIT License.
