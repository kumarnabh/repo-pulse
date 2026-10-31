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
repopulse.py clean   --repo PATH --ids ID1,ID2 [--review-ids ID2] [--allow-review] [--dry-run] [--permanent] [--dest DIR]
repopulse.py verify  --backup DIR
repopulse.py restore --backup DIR [--repo PATH] [--parts files,commits,stashes] [--overwrite] [--dry-run]
repopulse.py restore --bundle FILE --repo PATH
repopulse.py undo    --op OP_ID
repopulse.py ops     [--limit N]
repopulse.py recover [--fix]
repopulse.py backups [--verify] [--prune-days N [--apply]]
```

`restore` never moves an existing branch and never overwrites a file with uncommitted changes unless
`--overwrite` is given (the current version is first saved to `pre-restore-<time>/` in the backup).
Restored branch tips that clash with an existing branch are kept at `refs/repopulse-backup/<time>/<name>`,
outside `refs/remotes`, so they never make local work look pushed. Stashes are rebuilt as real stash
entries from their patch and recorded base commit when the original objects are gone.
`undo` reverses one journaled cleanup (branch, worktree with its ignored files, recycled folder); `git gc`,
pruned metadata and permanent deletes are irreversible and say so. `recover` lists cleanups that wrote a
`started` journal line but no `finished` line (crash, power loss, forced termination) with advice, plus
incomplete backups and stale locks.

`clean --review-ids` names the Review items the user approved one by one; every other id must still
be Safe when it is re-checked. `--allow-review` approves every listed id (command-line use only).
`--dry-run` re-checks the items and reports what would happen, the space and the recovery path,
without changing anything. `verify` re-hashes a backup against its manifest and re-verifies its bundle.

Files Repo Pulse writes outside its backup folder: `<git dir>/repopulse-fetch.json` (time of the last
successful and failed fetch per remote, written by `--fetch` and `sync`) and `<git dir>/repopulse.lock`
(held while a cleanup runs; ignored after 6 hours). Every backup and cleanup outcome is appended to
`<backup folder>/operations.jsonl`, with the recovery command where there is one.

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
| `fetched_age_h` | hours since the last fetch known to have succeeded; status is stale beyond 24 h |
| `errors` / `health` | git reads that failed (status, branches, worktrees, stashes, comparisons); `health` is `unknown` when any did |

The default branch is the remote's `HEAD`, else `init.defaultBranch`, else the first of `main`,
`master`, `trunk`, `develop` that exists. A fetch that Repo Pulse saw fail does not count as fresh
even though git rewrote `FETCH_HEAD`, until a later fetch succeeds.

## Safety classification

Every cleanup item is **Safe**, **Review** or **Keep**. Safe items run when selected; a Review item
runs only when its own id is approved (`--review-ids`). Immediately before **each** action the item
is classified again from scratch, so earlier actions in the same batch and other tools or agents are
accounted for: an item that no longer exists, changed identity, or turned from Safe to Review is
skipped. Only one cleanup runs per repository at a time.

### Branches
- **Keep**: the default branch; any branch checked out in any worktree; a branch being rebased;
  any branch with commits not contained in a remote-tracking ref.
- **Safe**: every commit is on a remote **and** the last fetch is under 24 hours old. The reason
  says whether the branch is merged into `origin/<default>` (`merged: true`) or only pushed.
- **Review**: contained in a remote but the fetch is stale; long-lived names (`develop`, `release/*`,
  `staging`, `production`, `hotfix/*`, …); or the upstream is gone and the commits are not on any
  remote (typical after a squash merge).
- Before deletion, every branch is written to a git bundle with a unique name
  (`branch-<name>-<hash>.bundle`), which must verify and carry the exact tip that was checked.
  Deletion is compare-and-delete (`git update-ref -d <ref> <checked tip>`): if another tool or agent
  moved the branch meanwhile, it is kept. Restore with `git fetch <bundle> <branch>:<branch>`.

### Worktrees
- **Safe**: the directory is already missing (prunable metadata), unless its detached HEAD is the
  only reference to some commits (then **Keep**). Only that one metadata entry is removed; other
  stale entries are left alone, unlike `git worktree prune`.
- **Safe**: clean, not locked, its HEAD is contained in a remote, with a fresh fetch, no git
  operation in progress, no file changed in the last 30 minutes, and it holds no ignored files other
  than rebuildable folders from the folder rules below.
- **Review**: as Safe, but it holds other ignored files (`.env`, local config, data). These are copied
  to the cleanup folder and hash-verified first; if any copy fails, the worktree is kept.
- **Keep**: dirty, locked, the main worktree, HEAD has unpushed commits, a merge/rebase/lock is in
  progress, or files changed in the last 30 minutes (a coding agent or editor may be using it;
  `REPOPULSE_ACTIVE_MINUTES` changes the window, 0 turns it off).
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
  garbage objects exceed 50 MB; **Keep** while a merge, rebase, bisect, lock or another gc is in
  progress. The scan size is an upper estimate; the result reports the space actually freed.

## Backup

Writes to `%USERPROFILE%\RepoPulse Backups\<repo>-<path hash>\<timestamp>[-N]\` (two repositories
with the same name, or two backups in the same second, never share a folder):
- every modified, staged and untracked (non-ignored) file, at its relative path, each copy
  hash-checked against the source and refused if the source changed while it was read;
- `changes.patch` (`git diff HEAD --binary`) and `staged.patch`;
- `unpushed.bundle` containing commits on no remote and the newest stash (skipped when there are none);
- `stash-N.patch` for every stash entry (`stash-0` is the newest), including untracked files on git 2.32+;
- `manifest.json` with branch, HEAD, file list, SHA-256 of every saved file, symlinks (recorded, never
  followed), problems, and restore instructions.

Before writing, free space is checked. After writing, every patch is test-applied to its base commit
in a throwaway index, and the bundle must verify and carry the stash tip. Any problem (a locked file,
a failed patch or bundle, a git read that failed) makes the result `ok: false, complete: false` with
the problems listed; what was saved stays on disk and an `INCOMPLETE` file marks the folder.

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
