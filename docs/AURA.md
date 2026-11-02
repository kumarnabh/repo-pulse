# Repo Pulse → Aura: read-only snapshot v1

Repo Pulse owns local Git observations. Aura may display them and suggest a next
step. The snapshot is **advice, never authorization to mutate a repository**.
The integration exposes no HTTP listener, action RPC, shell commands or secrets.

## CLI

Run a trusted installation with Python 3.11+ and Git on PATH:

```text
python -B repopulse.py snapshot --root "C:\Code" --no-hints --json
python -B repopulse.py snapshot --repo "C:\Code\app" --no-hints
python -B repopulse.py snapshot --root "C:\Code" --no-hints --previous previous.json
python -B repopulse.py snapshot --root "C:\Code" --no-hints --text
```

`--root`, `--repo` and `--exclude-path` accept literal paths and are repeatable.
Prefer these over the legacy semicolon-separated `--roots`/`--exclude`, especially
for paths containing semicolons. Pass arguments as an array with `shell=False`.
Explicit repositories include registered linked worktrees and their owner;
exclusions apply to all sources. Submodules are not independently inventoried.
Excluded checkouts are not inspected or offered for cleanup. Their registrations
still protect checked-out branches from deletion. Enclosing generated folders and
worktrees containing an excluded descendant are retained. Git status in an included
checkout still describes its complete dirty state, including tracked files below
excluded discovery folders.
Bare repositories are reported but cannot be resumed as working checkouts.
Folder discovery defaults to depth 6; `--depth` adjusts it, up to 64. Symlink and
junction directories are not traversed. Inventory completeness is within that
configured scope, not a claim to have searched every possible location.

Exactly one UTF-8 JSON object and a newline are written to stdout. `--json` is
the default; `--text` is for humans. No progress output contaminates JSON.
Exit codes: **0** complete snapshot (including unhealthy projects), **2** partial
snapshot, **1** invalid arguments, unsupported cache or execution failure. Help
and version use ordinary argparse text. Legacy `scan` retains exit 0 for partial
results so the existing widget remains compatible; inspect `complete` there too.

`snapshot` does not accept `--fetch`, `--push`, `--fix`, cleanup or restore flags.
Git reads disable optional index locks, prompts and partial-clone lazy fetching.
It does not write a snapshot/cache file: the caller owns persistence. `-B` also
prevents import bytecode files. A configured Git executable and local Git config
must be trusted; this API is not a sandbox for hostile Git installations.

## Python API and example adapter

```python
import repopulse

data = repopulse.snapshot(
    roots=[r"C:\Code"], exclude=[r"C:\Code\archive"],
    hints=False, workers=4, backup_dir=r"D:\RepoPulse Backups",
)
if data["complete"]:
    for checkout in data["repos"]:
        print(checkout["id"], checkout["readiness"]["resume"])
```

The API returns the same object; invalid arguments/read failures may raise an
exception. It has no mutation parameter. Aura should use explicit roots and
`hints=False` to constrain discovery. The CLI supports `--dest` to read an existing
backup journal; without it the default is `~/RepoPulse Backups`.

[examples/aura_client.py](../examples/aura_client.py) provides `read_health(core,
roots)`: a standard-library subprocess adapter with a timeout, strict complete /
read-only / schema checks, and a freshness limit. It never forwards arbitrary CLI
arguments. Configure `core` from a trusted installation, not repository content.
On failure, Aura should display an unknown state and retain the last snapshot only
as visibly dated history. Do not substitute a healthy empty project list.

## Schema and identity

[snapshot.schema.json](snapshot.schema.json) is the JSON Schema 2020-12 contract.
`schema_version` is independent of the application `version`. v1 changes are
additive; clients ignore unknown fields and reject unknown major versions.
Required fields do not change type within v1. Arrays can be empty; null means
unknown or inapplicable, never implicitly zero. Legacy widget counts may be zero
after a failed read, so inspect `health`, `errors` and `complete` before using them.

- `ok`: the scan ran. `complete`: all scoped discovery/Git/disk reads succeeded.
  Offline remotes do not make a read-only snapshot incomplete: connectivity is
  not tested. Fetch freshness and remote contact are separate observations.
- `scanned_at` / `observed_at`: Unix seconds; `scan_ms`: elapsed milliseconds.
  Reads run concurrently and do not form an atomic filesystem transaction.
- `id`: hash of canonical checkout path, stable across refresh and branch changes.
  `repository_id`: hash of the common Git directory, shared by linked worktrees.
  Moving a checkout or common directory changes the corresponding identity.
  Broken rows retain `id`, with `repository_id: null` and `error`.
- `branch`, `head`, `unborn`, `detached`, `upstream`, `branch_ahead/behind` describe
  the checkout. `ahead/behind` and `main_state` compare the selected default branch
  with the preferred remote (`origin`, else the first configured remote).
- `branches` lists local branch tips, checked-out paths, upstream counts/gone state,
  and `unpushed`: commits on no locally recorded remote-tracking ref. This differs
  from commits ahead of a particular upstream. No remote means reachability is
  unproven. `unpushed_branches` is the existing compact view.
- `dirty`: staged, modified, untracked, conflicted and total file counts. Categories
  can overlap. The preview lists at most 60 files; `truncated` marks the limit.
- `stashes` has `stash_scope: "repository"`: it is shared, repeated on linked rows.
  Deduplicate using `repository_id` when aggregating.
- `remotes`: every configured fetch/push URL with URL userinfo, query and fragment
  removed. Local paths, repository names and other diagnostic text can still be
  private; do not publish snapshots indiscriminately.
- `fresh` means a known successful fetch within 24 h. A later failed fetch
  invalidates it. Shared `FETCH_HEAD` is used only in a single-remote repository;
  multiple remotes require Repo Pulse's per-remote success record. `contact` is
  `failed` or `not_checked`; fresh refs never promise connectivity now.
  Timestamp comparison tolerates up to one minute of filesystem/tool clock skew.
- `worktree_inventory`: all registered worktrees, including missing and locked
  ones, with reason, stale/prunable metadata, branch, tip and cleanup status.
  Codex/Claude origins are **path heuristics**. `orphan_status: "missing"` means
  the registered folder is unavailable (it may be on an unmounted volume).
  `candidate` means an agent-looking checkout passed ordinary safe/review checks;
  it does not prove that the agent/session exited. `not_proven` is retained.
  No cleanup is authorized by an orphan label. Arbitrary folders without a Git
  marker or registered metadata are not identifiable as orphan worktrees.

## Risk, readiness and recovery

Risk levels are `healthy`, `protected` (local work to preserve), `review` (stale,
diverged or limited evidence), `blocked` (conflicts/locks/interrupted operations),
and `unknown` (failed reads). Findings have stable `code`, `reason`, `level` and
plain-language `guidance`. Unknown or blocked findings take priority.

Readiness has independent `resume`, `sync`, and `clean` decisions, each with
`state` (`safe`, `review`, `blocked`, `unknown`) and `reason`. Resuming a readable
dirty checkout can be safe while syncing it needs review. Sync refers to the
default branch, considers its holder, and still fetches/rechecks on invocation.
Clean's `safe_item_ids` identifies individual candidates, never permission to
delete a whole project. Locks, interrupted operations and failed reads suppress
these IDs. Every cleanup item retains the existing `safe` / `review` / `keep`
classification and now includes a recovery explanation.

`recovery` reports interrupted journal operations, incomplete backups and locks
without fixing anything. `interrupted_operations` links matching operations to
each checkout. Existing `recover`, `backup`, `verify`, `restore`, `undo`, `sync`
and `clean --dry-run` remain separate owner-invoked tools. Aura must never execute
diagnostic strings or recovery guidance as shell code.

## Refresh and disk estimates

`--workers` bounds concurrent repository reads (1–16, default 8; Python API 4).
Git status, refs, stashes, locks, remote freshness, discovery and all safety
classifications are read anew every time. A refresh reuses no readiness decision.

`--previous FILE` reuses only successful directory-size measurements for at most
`--max-size-age` seconds (default 300, maximum 3600). An incompatible or unreadable
previous snapshot fails explicitly. `--max-size-age 0` forces full measurement.
`refresh.reused_measurements`, per-row `disk.estimated` and each measurement's
`measured_at` expose provenance; reused sizes can differ from current disk use.
These estimates never feed cleanup authorization. The live cleanup path always
revalidates safety; reported sizes remain estimates. Source-tree discovery still
runs to find new candidates.

`totals.safe_bytes` and `review_bytes` deduplicate overlapping worktrees, generated
folders and caches, giving the enclosing candidate's risk band priority. GC bytes
are an upper estimate; filesystem allocation, compression, hardlinks, symlinks,
deferred Recycle Bin removal and shared package stores mean these are logical-size
estimates, not guaranteed immediately freed bytes. Unreadable measurements mark
the snapshot partial. A missing worktree contributes zero working-tree bytes;
prunable metadata is not advertised as recovered source data.
