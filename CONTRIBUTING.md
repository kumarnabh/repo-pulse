# Contributing

Issues and pull requests are welcome. Repo Pulse deletes things, so the bar is: **nothing that holds
unique work may ever be classified Safe.**

## Reporting a problem

Open an [issue](https://github.com/kumarnabh/repo-pulse/issues) with:

- the footer message from the widget (or the JSON `error` from the command line);
- `python repopulse.py --version`, `git --version` and your Windows version;
- for a wrong classification, the item's reason (hover it) and what the repository actually contained.

Please do not paste private paths or file contents you would not want public.

## Changing the code

- `repopulse.py` is standard library only and must keep running on Python 3.10.
- Every change to classification or cleanup needs a fixture test in `tests/test_repopulse.py` that
  builds the situation with real git commands and proves no false Safe.
- Run the tests before opening a pull request:

  ```
  python -m unittest discover -s tests -v
  ```

- The widget (`source/`) is code-only WPF on .NET 10. `Build.cmd` builds it; keep it free of warnings.

## Releasing (maintainers)

1. Set the same version in `repopulse.py` (`VERSION`) and `source/RepoPulse.csproj` (`<Version>`).
2. Add a `## x.y.z — YYYY-MM-DD` section to `CHANGELOG.md`.
3. Commit, then tag and push: `git tag vX.Y.Z && git push origin main vX.Y.Z`.

The Release workflow tests, builds both Windows downloads and publishes the release with the changelog
section as its notes.
