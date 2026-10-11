# Changelog

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
