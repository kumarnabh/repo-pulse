# Testing

## Automated

```
py -3 -m pip install -r requirements-dev.txt
py -3 -m unittest discover -s tests -v
```

The core itself remains standard-library only; the development dependency validates
the published JSON Schema. Without it only the schema-validation test is skipped.

`tests/test_snapshot.py` covers the CLI/Python/Aura contracts, immutable repository
bytes during snapshots, stable IDs, partial-result exit codes, failed/multiple
remotes, credential redaction, unusual literal paths, linked-only discovery, locks,
missing agent worktrees, incremental size provenance with live reclassification,
overlapping estimates, bare/unborn repositories and interrupted operations.
Newline/quote worktree paths run on Linux/macOS; Windows forbids such filenames.

On restricted Windows runners, set TEMP and TMP to a writable task-local scratch
directory. MinGit also requires Windows named-object access for local clone/fetch;
a sandbox refusal is an environment failure, not a Git regression.

### Performance observation (2026-11-03)

A disposable real-Git fixture with 2,000 ignored 1 KiB files across 40 package
directories required 67 Python directory reads for a full snapshot and 1 for a
refresh using the previous snapshot. Elapsed scan times on this Windows host were
2,378 ms and 1,904 ms, respectively; both reported 2,048,000 reclaimable bytes and
complete Git inventory. This is a single local measurement, not a general speed
guarantee. The regression test additionally proves cached sizes cannot conceal new
dirty/staged files, unpushed commits or newly tracked files in a cleanup candidate.

The WPF Release build was checked locally with .NET SDK 10.0.401: zero warnings and
zero errors. Native layout/interaction and Aura deployment require separate review;
the integration delivered here is the versioned producer and example consumer.

CI (`.github/workflows/ci.yml`) runs these on Windows, Linux and macOS with Python 3.11 and 3.14 and
builds the widget on Windows. Pushing a `v*` tag runs `.github/workflows/release.yml`, which tests
again, checks the tag matches both version numbers and publishes the downloads.

The suite creates real repositories and a local bare remote in a temp folder and checks:

| Area | Cases |
|---|---|
| Branches | merged, pushed, local-only, squash-merged (upstream gone), stale fetch, never fetched, no remote |
| Worktrees | clean+pushed, dirty, unpushed commit, folder deleted, locked, nested Claude-style worktree, ignored `.env` (Review, not removed) vs ignored `node_modules` (Safe) |
| Folders | ignored `node_modules` / `.venv`, not ignored, missing manifest, tracked files inside, generic `dist` |
| Discovery | linked worktree outside roots, submodule ignored, Claude Code / Codex / VS Code / Cursor recent list / JetBrains history, deleted hinted folder, `--no-hints`, `--exclude` |
| Worktree rows | parent, branch ahead of upstream, own `node_modules`, no branch or gc items |
| Re-verification | commit added after scan, file tracked after scan, Keep items with `--allow-review` |
| Sync | behind, main not checked out, diverged, dirty main, ahead with and without push |
| Backup | dirty files, patches, unpushed bundle, stash bundle, every stash as a patch, size limit |
| Status | renamed file line counts, Fetch + scan of a repo without a remote, `--text` report |

## Manual check on Windows (first run)

Use a throwaway clone for the destructive steps.

1. **Build**: run `Build.cmd`. It should end with `Built RepoPulse.exe`.
2. **Launch**: `Launch Repo Pulse.cmd`. The glass window opens and scans every fixed drive (or the ⚙ folders).
3. **Discovery**: with ⚙ folders empty, every repo on your fixed drives appears, plus Codex/Claude worktrees (⎇).
   Hover the summary line to see sources. Compare with the folders you expect.
3b. **Layout**: attention rows on top, healthy tiles below, no scrollbar; click a row or tile to expand;
   resize by hand, then double-click the header to fit again.
4. **Fetch + scan**: ahead/behind chips match `git status -sb` in each repo.
5. **Diff**: edit a file in a test repo → Diff → click the file → the viewer shows colored +/− lines.
6. **Backup**: Backup → open the folder → `files\`, `changes.patch`, `manifest.json` exist; the repo is unchanged.
7. **Sync**: in a test clone, `git reset --hard HEAD~1` on main → Fetch + scan → Sync → main is back in sync.
8. **Space**: create a merged branch and a `node_modules` folder in a test repo → Fetch + scan → both show Safe → Clean → the branch bundle exists in the backup folder, `node_modules` is in the Recycle Bin.
9. **Long paths**: a deep `node_modules` may be refused by the Recycle Bin; the widget should offer permanent deletion for Safe items only.
10. **Window**: drag by the header, resize from the corner grip, minimize, close.

Report failures with the footer message and, for build errors, the full `Build.cmd` output.
