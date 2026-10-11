# Security

Repo Pulse runs locally, makes no network calls of its own (only `git fetch` / `git push` when you ask)
and never asks for credentials.

If you find a way it could delete or expose data it should not (for example an item wrongly marked
Safe, or a path that escapes a repository), please report it privately through
[GitHub security advisories](https://github.com/kumarnabh/repo-pulse/security/advisories/new)
rather than a public issue. Fixes for data-loss bugs take priority over everything else.
