# Testing

## Automated

```
py -3 -m unittest discover -s tests -v
```

CI (`.github/workflows/ci.yml`) runs these on Windows and Linux and builds the widget on Windows.

The suite creates real repositories and a local bare remote in a temp folder and checks:

| Area | Cases |
|---|---|
| Branches | merged, pushed, local-only, squash-merged (upstream gone), stale fetch, never fetched, no remote |
| Worktrees | clean+pushed, dirty, unpushed commit, folder deleted, locked, nested Claude-style worktree |
| Folders | ignored `node_modules` / `.venv`, not ignored, missing manifest, tracked files inside, generic `dist` |
| Re-verification | commit added after scan, file tracked after scan, Keep items with `--allow-review` |
| Sync | behind, main not checked out, diverged, dirty main, ahead with and without push |
| Backup | dirty files, patches, unpushed bundle, stash bundle, size limit |

## Manual check on Windows (first run)

Use a throwaway clone for the destructive steps.

1. **Build**: run `Build.cmd`. It should end with `Built RepoPulse.exe`.
2. **Launch**: `Launch Repo Pulse.cmd`. The glass window opens and scans the user folder.
3. **Settings**: ⚙ → set your code folders → Save. The scan should list your repos.
4. **Fetch + scan**: ahead/behind chips match `git status -sb` in each repo.
5. **Diff**: edit a file in a test repo → Diff → click the file → the viewer shows colored +/− lines.
6. **Backup**: Backup → open the folder → `files\`, `changes.patch`, `manifest.json` exist; the repo is unchanged.
7. **Sync**: in a test clone, `git reset --hard HEAD~1` on main → Fetch + scan → Sync → main is back in sync.
8. **Space**: create a merged branch and a `node_modules` folder in a test repo → Fetch + scan → both show Safe → Clean → the branch bundle exists in the backup folder, `node_modules` is in the Recycle Bin.
9. **Long paths**: a deep `node_modules` may be refused by the Recycle Bin; the widget should offer permanent deletion for Safe items only.
10. **Window**: drag by the header, resize from the corner grip, minimize, close.

Report failures with the footer message and, for build errors, the full `Build.cmd` output.
