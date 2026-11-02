# Audit before implementation — 2026-11-02

Audited checkout: `cfb908425ac73f1d0362db6cb60892eb2b777635`.
The clean local `main` includes `d145222` and `cfb9084` beyond GitHub
`main` (`2b5eda4`). These existing safety/recovery commits are prerequisites
and must be preserved in the PR history.

## Existing behavior

- Standard-library Python core and WPF client; scan JSON is unversioned.
- Discovery walks roots, tool histories and linked worktrees; eight concurrent
  repository scans, branch-wide unpushed-commit batching and shared tree sizes.
- Branch, dirty-file, stash, remote-health and cleanup classification already exist.
- Cleanup revalidates each selection, backs up branch tips/ignored files, uses
  compare-and-delete and refuses forced worktree removal. Backup verification,
  restore, operation journal, undo and crash recovery are already implemented.
- Existing suite: 72 real-Git tests (including platform-specific cases).

## Gaps found in source

- No stable schema, identity, completeness contract or dedicated read-only entry
  point for integrations; partial failures still report top-level `ok: true`.
- Branch/worktree facts are mixed into cleanup items; no complete remote inventory
  or distinct readiness/recovery assessments.
- Worktree list parsing splits on newlines, which misreads unusual Unix paths.
- Discovery silently skips inaccessible/missing roots and excluded root contents
  can still be walked. A linked-only discovery does not recover its owning repo.
- Fetch freshness can retain an older success after a subsequent failed fetch;
  shared FETCH_HEAD cannot prove freshness of a particular remote.
- No incremental disk-size reuse, agent provenance hints, or honest orphan
  confidence. Quiet directories alone cannot prove an agent has exited.
- Sync substitutes zero comparisons after a failed read and does not explicitly
  refuse an unreadable status or an interrupted operation before fast-forward.

## Implementation boundary

Add a versioned, additive snapshot to the existing core; preserve action commands
and widget compatibility. Always reread Git and reclassify safety. Reuse only
bounded-age disk estimates, with provenance. Treat agent attribution as a path
heuristic and orphanhood as a candidate unless the folder is missing. Expose
reasons and recovery guidance; never execute advice from the integration.

Initial baseline execution hit Windows sandbox restrictions (temporary-directory
permissions, then MinGit named-object access). Validation uses workspace-local
scratch and an approved unsandboxed test process; results are recorded in TESTING.md.
