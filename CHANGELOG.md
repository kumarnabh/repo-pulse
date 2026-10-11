# Changelog

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
