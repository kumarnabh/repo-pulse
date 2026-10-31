# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); releases are on
[GitHub](https://github.com/kumarnabh/repo-pulse/releases).

## 0.5.0 — unreleased

Safety, backup and recovery reliability.

### Safety fixes
- **Review items need their own approval.** The widget used to pass `--allow-review` for a whole repository whenever one selected item was Review, so an item selected as Safe that turned Review before cleanup (for example, the fetch went stale) was removed anyway. Review items are now approved one by one (`--review-ids`).
- **Every item is re-checked right before its own action**, not once per batch, so earlier removals in the same batch and other tools are accounted for. Only one cleanup runs per repository at a time.
- **Compare-and-delete for branches.** A branch is deleted only if it still points at the commit that was checked and bundled; if a coding agent commits to it meanwhile, it is kept.
- **Ignored files are saved before a worktree is removed.** `.env`, local config and data files in a Review worktree are copied out and hash-verified first; if a copy fails, the worktree is kept.
- **Worktrees in use are kept.** A worktree with files changed in the last 30 minutes, or with a merge, rebase or index lock in progress, is never offered.
- **Stale worktree metadata is pruned one entry at a time.** `git worktree prune` removed every stale entry, including ones not selected. A stale detached worktree whose HEAD is the only reference to some commits is now kept.
- Branches being rebased, and long-lived names (`develop`, `release/*`, `staging`, …), are never Safe. Safe branches now say whether they are merged into the default branch or only pushed.
- `git gc` is held while another git operation is running, and reports the space it actually freed instead of the estimate.
- Links and junctions are never followed by backup, folder or cache cleanup.

### Backup fixes
- **Backups never overwrite each other.** Two repositories with the same name shared one backup folder, two backups in the same second collided, and branches such as `feat/x` and `feat_x` overwrote each other's bundle. Folders and bundles now have unique names.
- **No false success.** A backup that misses a file, a patch, a stash or the bundle now reports `INCOMPLETE` (and the widget says so) instead of success.
- **Backups are proven restorable.** Every file copy is hash-checked against its source (and refused if the source changed meanwhile), every patch is test-applied in a throwaway index, and the bundle must verify. Free space is checked first. New `verify` command re-checks an old backup.
- Every backup and cleanup is recorded in `operations.jsonl` with its recovery command.

### Bug fixes
- A failed fetch no longer counts as fresh because git rewrote `FETCH_HEAD`.
- Repositories whose default branch is `trunk`, `develop` or `init.defaultBranch` are recognised.
- Failed git reads (status, branches, worktrees, stashes, comparisons) show as errors and `health: unknown` instead of a healthy-looking repository.
- New untracked folders list each file, so counts, diffs and backups see real files.
- Every existing global cache location is found, not only the first.
- New `clean --dry-run` reports what would be removed, the space and how to recover it, without changing anything.
- Git for Windows silently skipped paths over 260 characters, so a backup could miss them and still report success. Every git call now enables `core.longpaths` on Windows.

### Recovery
- **`restore`** puts a backup back: files (a file with uncommitted changes that differ from the backup is never overwritten without `--overwrite`, and then your version is saved first), branches (missing ones are recreated; an existing branch is never moved, the backed-up tip is kept at `refs/repopulse-backup/…`), and every stash, rebuilt as real stash entries even in a fresh clone. `restore --bundle` brings back a branch removed by cleanup; `restore --backup <worktree-…>` puts saved ignored files back.
- **`undo --op ID`** reverses a cleanup: deleted branches, removed worktrees (with their ignored files) and folders moved to the Recycle Bin. Irreversible actions (`git gc`, pruned metadata, permanent deletes) are marked as such and refused.
- **Operation journal.** Every cleanup writes a `started` line before it changes anything and a `finished` line after, with the previous ref, outcome, freed space and recovery data. `ops` lists them; **`recover`** finds operations cut short by a crash or power loss, says what to check, and with `--fix` removes stale locks left by dead processes.
- **`backups`** lists every backup and cleanup folder with completeness, encryption, size, age and restore command; `--verify` re-hashes them; `--prune-days N [--apply]` is a retention policy that keeps the newest per repository and moves the rest to the Recycle Bin.
- **Encrypted backups** (`backup --encrypt`, `clean --encrypt`): copied files, patches and saved ignored files are encrypted with Windows DPAPI for the current Windows account, and each one is decrypted again to prove it.
- Backup manifests now record each stash's base commit and message. The old restore hint (fetching into `refs/remotes/backup/*`) is replaced: refs under `refs/remotes` would make unpushed work look pushed.

### Repository layouts
- Shallow clones, partial clones, sparse checkout, Git LFS and submodules are detected (`features`). Branches of a shallow clone are never Safe; worktrees with initialized submodules are kept; a backup of an LFS repository with unpushed commits says the LFS contents are not in the bundle.
- **Remote health**: branches tracking a remote that does not exist, remotes without a URL, local remotes whose folder is gone, and an unset `origin/HEAD` are reported.
- A detached HEAD with commits on no branch is flagged so they are not lost.

### Runtime
- Python 3.11 or newer is required (3.10 reached end of life in October 2026).

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
