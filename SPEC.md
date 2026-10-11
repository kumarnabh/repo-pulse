# Repo Pulse — Specification

Local git health and space manager for Windows. Same architecture as Prism:
a .NET 10 WPF glass shell that calls a Python core which prints JSON.

## Goals

1. **Status**: for every local repo, show whether local `main` agrees with
   `origin/main`, dirty files with a diff preview, unpushed commits and stashes.
2. **Protect work**: one-click backup of dirty files and unpushed commits.
3. **Sync safely**: fast-forward only. Push only without force.
4. **Space**: find reclaimable space (merged/synced branches, stale worktrees,
   rebuildable folders, git garbage) and remove it safely.

Non-goals: merging, rebasing, conflict resolution, force-push, scheduled or
automatic cleanup, anything requiring administrator rights.

## Architecture

| Layer | File | Role |
|---|---|---|
| Core | `repopulse.py` | Discovery, scan, classification, actions. Standard library only. Prints one JSON object per call. |
| Shell | `source/*.cs` | WPF widget: Status and Space tabs, confirmations, calls the core. |
| Data | `data/settings.json` | Scan roots, backup folder. No credentials. |

Core commands:

```
repopulse.py scan    --roots "D:\Code;C:\Users\me\source" [--exclude "D:\Code\old"] [--fetch] [--caches] [--no-hints] [--text]
repopulse.py backup  --repo PATH [--dest DIR]
repopulse.py sync    --repo PATH [--push]
repopulse.py clean   --repo PATH --ids ID1,ID2 [--allow-review] [--dest DIR]
```

`scan` never changes anything, except `--fetch`, which runs
`git fetch --prune --no-tags origin` on every main repository before scanning (it updates only
remote-tracking refs; repositories without a remote are skipped, not reported as failures).
`--no-hints` skips tool-history discovery. `--exclude` lists folders that are never walked or reported.
`--text` prints a readable report instead of JSON.

## Widget layout

- **Status**: repositories needing attention as one-line rows (status dot, name, compact chips;
  click to expand details, files and actions). Healthy repositories as a tile grid. Linked
  worktrees are nested under their repository.
- **Space**: one line per item with size; the reason is in the tooltip.
- The window fits its height to the content (up to the screen's work area) and hides the
  scrollbar; the mouse wheel still scrolls if content exceeds the screen. Resizing by hand turns
  fitting off; double-clicking the header turns it back on.

## Repo status

| Field | Meaning |
|---|---|
| `kind` | `repo` or `worktree` (with `parent`, `parent_name`) |
| `main_state` | `in_sync`, `behind` (fast-forwardable), `ahead` (unpushed), `diverged`, `no_remote`, `no_main`, `worktree` |
| `branch_ahead` / `branch_behind` | current branch vs its upstream; never-pushed branches count commits on no remote |
| `ahead` / `behind` | commit counts between local default branch and `origin/<default>` |
| `dirty` | staged, modified, untracked, conflicted counts; `diff` = per-file +/− lines |
| `unpushed_branches` | local branches whose commits exist on no remote |
| `stashes` | stash count (stashes are never touched by cleanup) |
| `fetched_age_h` | hours since the last fetch; status is stale beyond 24 h |

## Safety classification

Every cleanup item is **Safe**, **Review** or **Keep**. Only Safe items can be
removed without `--allow-review`. Classification is recomputed immediately
before execution; if an item changed since the scan, it is skipped.

### Branches
- **Keep**: the default branch; any branch checked out in any worktree; any branch
  with commits not contained in a remote-tracking ref.
- **Safe**: the tip is contained in `origin/<default>` or in its own upstream,
  **and** the last fetch is under 24 hours old.
- **Review**: contained in a remote but the fetch is stale; or the upstream is gone
  and the commits are not on any remote (typical after a squash merge).
- Before deletion, every branch is written to a `git bundle` in the backup
  folder, so it can be restored with `git fetch <bundle> <branch>:<branch>`.

### Worktrees
- **Safe**: the directory is already missing (prunable metadata) → `git worktree prune`.
- **Safe**: clean, not locked, its HEAD is contained in a remote, with a fresh fetch, and it holds
  no ignored files other than rebuildable folders from the folder rules below.
- **Review**: as Safe, but it holds other ignored files (`.env`, local config, data). `git worktree
  remove` deletes ignored files without asking, and these exist nowhere else.
- **Keep**: dirty, locked, the main worktree, or HEAD has unpushed commits.
- Removal uses `git worktree remove` without `--force`, so git itself refuses
  dirty trees as a second guard.

### Folders
- **Safe**: a known rebuildable folder (`node_modules`, `.venv`, `__pycache__`,
  `.next`, `bin/obj` next to a `.csproj`, `target` next to `Cargo.toml`, …) that
  is git-ignored, has no tracked files, and whose manifest exists
  (`package.json`, `pyproject.toml`/`requirements*.txt`, etc.).
- **Review**: rebuildable-looking but not ignored, or the manifest is missing;
  generic `dist`/`build`/`out` folders.
- **Never**: anything containing tracked files.
- Folders go to the Recycle Bin. Windows warns before permanently deleting
  anything too large for the bin.

### Git garbage
- `git gc` (default prune window, never `--prune=now`) is Safe when loose or
  garbage objects exceed 50 MB.

## Backup

Writes to `%USERPROFILE%\RepoPulse Backups\<repo>\<timestamp>\`:
- every modified, staged and untracked (non-ignored) file, at its relative path;
- `changes.patch` (`git diff HEAD --binary`) and `staged.patch`;
- `unpushed.bundle` containing commits on no remote and the newest stash (skipped when there are none);
- `stash-N.patch` for every stash entry (`stash-0` is the newest), including untracked files on git 2.32+;
- `manifest.json` with branch, HEAD, file list, files that could not be copied, and restore instructions.

A file that cannot be read (for example locked by another program) is listed in `failed`; the rest
of the backup continues.

The working tree is not modified.

## Sync

- Default branch behind and not diverged: fast-forward only. If it is checked out
  in a clean worktree, `git merge --ff-only`; if not checked out, `git fetch . origin/main:main`.
  Git refuses non-fast-forward updates in both paths.
- `--push`: `git push origin <branch>` without force, only when ahead and not behind.
- Diverged or dirty: no action; the UI shows the diff and the counts.

## Repository discovery

Three sources, merged and de-duplicated by real path:

1. **Folders**: each root is walked to depth 6, skipping system folders, AppData, package caches
   and dependency folders. With no roots configured, every fixed drive is a root on Windows
   (the home folder elsewhere). Network and removable drives are never walked.
2. **Tool history**: folders recorded by Claude Code (`~/.claude.json` projects), Codex
   (`~/.codex/config.toml` projects), VS Code / Cursor / Windsurf (`storage.json` workspace URIs and
   the recently opened list in `state.vscdb`, opened read-only) and JetBrains IDEs
   (`options/recentProjects.xml`). Each existing path is resolved to its enclosing repository; paths
   that no longer exist are ignored. Disable with `--no-hints`.

Excluded folders (`--exclude`) apply to all three sources.
3. **Worktree lists**: every linked worktree of every found repository, wherever it lives
   (for example `~/.codex/worktrees` or `.claude/worktrees`).

A `.git` directory marks a main repository; a `.git` file pointing into `…/worktrees/` marks a
linked worktree, which gets its own row. Submodules are not listed.

Linked-worktree rows report their branch against its upstream and their own rebuildable folders;
branches, worktree cleanup and `git gc` stay on the main repository row. Totals count a worktree's
folders once, even when the worktree itself is a Safe cleanup item.

Junk detection walks each worktree to depth 6 and does not descend into nested repositories or worktrees.

## Global caches (optional)

npm, pip and Yarn caches are Safe (re-downloaded on demand). The pnpm store, NuGet packages
and Playwright browsers are Review (rebuild needs the network or a prune command).

## Acceptance

- Scanner reports reclaimable bytes per repo and in total.
- Zero false Safe flags in the fixture test suite (unpushed commits, stale fetch,
  dirty worktree, tracked files inside junk folders, squash-merged branches).
