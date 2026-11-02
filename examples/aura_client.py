"""Read-only subprocess adapter for Aura. Python 3.11+, standard library only."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


def read_health(core: str, roots: list[str], *, timeout: float = 120, max_age: float = 300) -> dict:
    """Return a complete v1 snapshot or fail closed. Never accepts arbitrary core arguments.

    `core` must come from trusted Aura configuration, not a repository or model output.
    An incomplete result is available as JSON from the CLI for diagnostic displays;
    this stricter adapter refuses to turn it into project readiness decisions.
    """
    if not roots:
        raise ValueError("Configure explicit local roots for Aura")
    argv = [sys.executable, "-B", str(Path(core).resolve()), "snapshot", "--no-hints", "--json"]
    for root in roots:
        argv.extend(("--root", root))
    try:
        process = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"Repo Pulse could not finish: {exc}") from exc
    try:
        data = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SnapshotError("Repo Pulse returned invalid JSON") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("schema_version"), str) or
            data["schema_version"].split(".")[0] != "1"):
        raise SnapshotError("Unsupported Repo Pulse snapshot schema")
    if process.returncode != 0 or data.get("ok") is not True or data.get("complete") is not True or data.get("read_only") is not True:
        raise SnapshotError("Repo Pulse snapshot is incomplete or failed; inspect its diagnostics")
    observed = data.get("scanned_at")
    if type(observed) not in (int, float) or not 0 <= time.time() - observed <= max_age:
        raise SnapshotError("Repo Pulse snapshot is stale or has an invalid timestamp")
    if not isinstance(data.get("repos"), list) or any(not isinstance(r, dict) or not isinstance(r.get("readiness"), dict) for r in data["repos"]):
        raise SnapshotError("Repo Pulse snapshot is missing readiness data")
    return data
