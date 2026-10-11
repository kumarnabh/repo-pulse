# Troubleshooting

**"Python 3.10 or newer was not found"**
Install Python from python.org with the Python launcher (`py`). The Microsoft Store alias (`WindowsApps\python.exe`) is not used.

**Build fails: `dotnet` not found**
Install the .NET 8 SDK, open a new terminal, run `Build.cmd` again.

**A repo shows "Not a readable git repository"**
Usually git's ownership check. Run `git config --global --add safe.directory <path>` if you trust that folder.

**Ahead/behind looks wrong, or branches show Review instead of Safe**
The last fetch is older than 24 hours. Use **Fetch + scan**.

**Fetch failed**
Credentials are never requested by Repo Pulse. Sign in once with your normal git tooling (for example `git fetch` in a terminal) and retry.

**Recycle Bin refused**
Windows' Recycle Bin cannot take some very long paths (common in `node_modules`). For Safe rebuildable folders the widget offers permanent deletion; anything else is left in place.

**Sync stopped safely**
The branch has diverged, or uncommitted changes would be overwritten. Repo Pulse never merges or rebases; resolve it in your editor or terminal.

**Backup refused: above the size limit**
Large untracked assets (for example Blender files) exceed 2 GB. Commit or ignore them, or run
`py -3 repopulse.py backup --repo <path> --max-gb 10`.

**Scanning is slow**
Point ⚙ settings at your code folders instead of the whole user folder.
