# Troubleshooting

**Windows protected your PC (SmartScreen)**
Releases are not code-signed yet. Choose **More info → Run anyway**, or build from source.
Compare the zip against `SHA256SUMS.txt` on the release page: `Get-FileHash RepoPulse-*.zip`.

**"Repo Pulse core did not finish" / nothing happens after unzipping**
Keep `repopulse.py` next to `RepoPulse.exe`, and unzip to a folder you can write to (not `Program Files`),
because settings are saved in `data\` beside the program.

**"Python 3.10 or newer was not found"**
Install Python from python.org with the Python launcher (`py`). The Microsoft Store alias (`WindowsApps\python.exe`) is not used.

**Build fails: `dotnet` not found**
Install the .NET 10 SDK (`winget install Microsoft.DotNet.SDK.10`), open a new terminal, run `Build.cmd` again.

**A repository is missing**
Hover over the summary line to see which folders were searched and how each repository was found.
If ⚙ settings list specific folders, only those (plus Claude Code / Codex / VS Code history and
worktrees) are searched; clear the field to scan every fixed drive. Network and removable drives,
and folders deeper than 6 levels below a root, are not walked; add them as roots.

**Too many repositories, or ones you don't care about**
List them under "Folders to skip" in ⚙ settings (or `scan --exclude`). Skipped folders and everything
inside them are never searched or shown, even when an editor remembers them.

**A clean, pushed worktree shows Review instead of Safe**
It holds ignored files such as `.env` or local config, which `git worktree remove` would delete with no
copy anywhere else. Hover the item to see which files. Move what you need, or select it knowingly.

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
The first scan of every drive takes longest. Point ⚙ settings at your code folders to speed it up;
tool-history folders and worktrees are still included.

**The window is too tall or too short**
It fits itself to the content. If you resized it by hand, double-click the header to fit again.
