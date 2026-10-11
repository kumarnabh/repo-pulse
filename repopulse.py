#!/usr/bin/env python3
# Copyright (c) 2026 Nabh Kumar. MIT License.
"""Repo Pulse core: local git health and space manager.

Standard library only. Every command prints exactly one JSON object to stdout.
`scan` is read-only (except `--fetch`, which only updates remote-tracking refs).
Actions re-verify every item immediately before changing anything.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

VERSION = "0.1.0"
IS_WIN = os.name == "nt"
FRESH_HOURS = 24
GC_THRESHOLD = 50 * 1024 * 1024
MAX_DEPTH = 5
JUNK_DEPTH = 6
DIFF_FILES = 60
DIFF_TEXT_LIMIT = 200_000

SKIP_WALK = {
    "node_modules", ".venv", "venv", "__pycache__", ".git", "$recycle.bin",
    "system volume information", "windows", "program files", "program files (x86)",
    "programdata", "appdata", "library", ".cache", ".npm", ".nuget", ".gradle",
    ".cargo", ".rustup", "anaconda3", "miniconda3",
}
PY_MANIFESTS = ["pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "uv.lock"]
NODE = ["package.json"]
DOTNET = ["*.csproj", "*.fsproj", "*.vbproj", "*.sln"]

# name -> (label, manifests or None when always regenerable, generic)
JUNK_RULES = {
    "node_modules": ("Node packages", NODE, False),
    ".venv": ("Python virtual env", PY_MANIFESTS, False),
    "venv": ("Python virtual env", PY_MANIFESTS, False),
    "__pycache__": ("Python bytecode", None, False),
    ".pytest_cache": ("pytest cache", None, False),
    ".mypy_cache": ("mypy cache", None, False),
    ".ruff_cache": ("ruff cache", None, False),
    ".tox": ("tox envs", ["tox.ini", "pyproject.toml"], False),
    ".next": ("Next.js build", NODE, False),
    ".nuxt": ("Nuxt build", NODE, False),
    ".svelte-kit": ("SvelteKit build", NODE, False),
    ".turbo": ("Turbo cache", NODE, False),
    ".parcel-cache": ("Parcel cache", NODE, False),
    ".angular": ("Angular cache", NODE, False),
    ".vite": ("Vite cache", NODE, False),
    ".wrangler": ("Wrangler local state", ["wrangler.toml", "wrangler.json", "wrangler.jsonc"], True),
    "coverage": ("Coverage report", NODE + PY_MANIFESTS, True),
    ".nyc_output": ("Coverage data", NODE, False),
    "target": ("Rust build", ["Cargo.toml"], False),
    "obj": (".NET intermediate", DOTNET, False),
    "bin": (".NET build", DOTNET, False),
    "dist": ("Build output", None, True),
    "build": ("Build output", None, True),
    "out": ("Build output", None, True),
}

CACHE_DIRS = [
    ("npm cache", ["%LOCALAPPDATA%/npm-cache", "~/.npm/_cacache"], "safe"),
    ("pip cache", ["%LOCALAPPDATA%/pip/Cache", "~/.cache/pip"], "safe"),
    ("Yarn cache", ["%LOCALAPPDATA%/Yarn/Cache", "~/.cache/yarn"], "safe"),
    ("pnpm store", ["%LOCALAPPDATA%/pnpm/store", "~/.local/share/pnpm/store"], "review"),
    ("NuGet packages", ["~/.nuget/packages"], "review"),
    ("Playwright browsers", ["%LOCALAPPDATA%/ms-playwright", "~/.cache/ms-playwright"], "review"),
]


# ---------------------------------------------------------------- helpers

def now() -> float:
    return time.time()


def item_id(kind: str, repo: str, target: str) -> str:
    return hashlib.sha1(f"{kind}|{os.path.normcase(repo)}|{target}".encode("utf-8")).hexdigest()[:12]


def longp(path: str | Path) -> str:
    p = os.path.abspath(str(path))
    if IS_WIN and not p.startswith("\\\\?\\") and len(p) > 240:
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def git(repo: str | Path, *args: str, timeout: float = 60, stdin: bytes | None = None) -> tuple[int, str, str]:
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GCM_INTERACTIVE="never", LC_ALL="C")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {int(timeout)}s"
    except FileNotFoundError:
        raise RuntimeError("Git is not installed or not on PATH.")
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace").strip()


def git_bytes(repo: str | Path, *args: str, timeout: float = 60) -> tuple[int, bytes]:
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GCM_INTERACTIVE="never")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        return 124, b""
    return r.returncode, r.stdout


def git_ok(repo, *args, timeout=60) -> str | None:
    code, out, _ = git(repo, *args, timeout=timeout)
    return out if code == 0 else None


def dir_size(path: str | Path, limit_depth: int = 64) -> int:
    total = 0
    stack = [(longp(path), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for e in it:
                    try:
                        if is_link_like(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if depth < limit_depth:
                                stack.append((e.path, depth + 1))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def is_link_like(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    junction = getattr(entry, "is_junction", None)
    return bool(junction and junction())


def has_manifest(folder: Path, manifests: list[str] | None) -> bool:
    if manifests is None:
        return True
    for m in manifests:
        if "*" in m:
            if any(folder.glob(m)):
                return True
        elif (folder / m).exists():
            return True
    return False


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def expand(p: str) -> Path | None:
    if "%LOCALAPPDATA%" in p and not os.environ.get("LOCALAPPDATA"):
        return None
    return Path(os.path.expanduser(os.path.expandvars(p)))


def default_roots() -> list[str]:
    return [str(Path.home())]


def backup_root(dest: str | None) -> Path:
    return Path(dest) if dest else Path.home() / "RepoPulse Backups"


# ---------------------------------------------------------------- discovery

def discover(roots: list[str], max_depth: int = MAX_DEPTH) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            dotgit = os.path.join(current, ".git")
            if os.path.isdir(dotgit):  # main worktree; .git files are linked worktrees/submodules
                key = os.path.normcase(os.path.realpath(current))
                if key not in seen:
                    seen.add(key)
                    found.append(current)
            if depth >= max_depth:
                continue
            try:
                with os.scandir(current) as it:
                    for e in it:
                        try:
                            if not e.is_dir(follow_symlinks=False) or is_link_like(e):
                                continue
                        except OSError:
                            continue
                        name = e.name.lower()
                        if name in SKIP_WALK or name.startswith("$"):
                            continue
                        stack.append((e.path, depth + 1))
            except OSError:
                continue
    return sorted(found, key=lambda p: p.lower())


# ---------------------------------------------------------------- repo facts

class Repo:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path.rstrip("\\/")) or self.path
        common = git_ok(self.path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is None:
            raise RuntimeError("Not a readable git repository (check safe.directory or permissions).")
        self.git_dir = common.strip()
        remotes = (git_ok(self.path, "remote") or "").split()
        self.remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        self.default = self._default_branch()
        self.has_head = git_ok(self.path, "rev-parse", "--verify", "-q", "HEAD") is not None

    def _default_branch(self) -> str | None:
        if self.remote:
            sym = git_ok(self.path, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{self.remote}/HEAD")
            if sym and "/" in sym.strip():
                return sym.strip().split("/", 1)[1]
            for b in ("main", "master"):
                if self.ref_exists(f"refs/remotes/{self.remote}/{b}"):
                    return b
        for b in ("main", "master"):
            if self.ref_exists(f"refs/heads/{b}"):
                return b
        cur = git_ok(self.path, "symbolic-ref", "--quiet", "--short", "HEAD")
        return cur.strip() if cur else None

    def ref_exists(self, ref: str) -> bool:
        return git_ok(self.path, "rev-parse", "--verify", "-q", ref) is not None

    def fetched_age_h(self) -> float | None:
        fh = os.path.join(self.git_dir, "FETCH_HEAD")
        try:
            return round((now() - os.path.getmtime(fh)) / 3600, 2)
        except OSError:
            return None

    def fresh(self) -> bool:
        age = self.fetched_age_h()
        return age is not None and age <= FRESH_HOURS

    def unpushed(self, rev: str) -> int | None:
        if not self.remote:
            return None
        out = git_ok(self.path, "rev-list", "--count", rev, "--not", "--remotes")
        return int(out.strip()) if out else None

    def fetch(self, timeout: float = 45) -> str | None:
        if not self.remote:
            return "no remote"
        code, _, err = git(self.path, "fetch", "--prune", "--no-tags", "--quiet", self.remote, timeout=timeout)
        return None if code == 0 else (err.splitlines()[-1] if err else f"fetch failed ({code})")

    def worktrees(self) -> list[dict]:
        out = git_ok(self.path, "worktree", "list", "--porcelain") or ""
        trees, cur = [], None
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = {"path": line[9:], "head": None, "branch": None, "locked": False, "prunable": False, "bare": False}
                trees.append(cur)
            elif cur is None:
                continue
            elif line.startswith("HEAD "):
                cur["head"] = line[5:]
            elif line.startswith("branch "):
                cur["branch"] = line[7:].removeprefix("refs/heads/")
            elif line == "bare":
                cur["bare"] = True
            elif line.startswith("locked"):
                cur["locked"] = True
            elif line.startswith("prunable"):
                cur["prunable"] = True
        for i, t in enumerate(trees):
            t["main"] = i == 0
        return trees


def parse_status(text: str) -> dict:
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicted": 0}
    files: list[dict] = []
    parts = text.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1  # skip the original path
        if xy == "??":
            counts["untracked"] += 1
            kind = "untracked"
        elif "U" in xy or xy in ("AA", "DD"):
            counts["conflicted"] += 1
            kind = "conflicted"
        else:
            if xy[0] not in " ?":
                counts["staged"] += 1
            if xy[1] not in " ?":
                counts["modified"] += 1
            kind = "deleted" if "D" in xy else ("added" if "A" in xy else "modified")
        files.append({"path": path, "xy": xy, "kind": kind})
    counts["total"] = len(files)
    return {"counts": counts, "files": files}


def dirty_state(path: str) -> dict:
    code, out, err = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", timeout=90)
    if code != 0:
        return {"counts": {"total": 0}, "files": [], "error": err or "status failed"}
    return parse_status(out)


def numstat(repo: Repo) -> dict[str, tuple]:
    if not repo.has_head:
        return {}
    out = git_ok(repo.path, "diff", "--numstat", "HEAD") or ""
    stats = {}
    for line in out.splitlines():
        bits = line.split("\t", 2)
        if len(bits) == 3:
            add, rem, p = bits
            if " => " in p:
                p = p.split(" => ")[-1].replace("}", "").replace("{", "")
            stats[p] = (None if add == "-" else int(add), None if rem == "-" else int(rem))
    return stats


# ---------------------------------------------------------------- classification

def classify_branches(repo: Repo, trees: list[dict]) -> list[dict]:
    checked_out = {t["branch"] for t in trees if t.get("branch")}
    fmt = "%(refname:short)%00%(upstream:short)%00%(upstream:track)%00%(objectname)%00%(committerdate:unix)"
    out = git_ok(repo.path, "for-each-ref", f"--format={fmt}", "refs/heads") or ""
    fresh = repo.fresh()
    items = []
    for line in out.splitlines():
        name, upstream, track, sha, when = (line.split("\0") + ["", "", "", "", "0"])[:5]
        unpushed = repo.unpushed(f"refs/heads/{name}")
        gone = "[gone]" in track
        base = {
            "kind": "branch", "target": name, "upstream": upstream or None, "upstream_gone": gone,
            "head": sha, "unpushed": unpushed, "last_commit": int(when or 0), "bytes": 0,
        }
        if name == repo.default:
            status, reason = "keep", "Default branch"
        elif name in checked_out:
            status, reason = "keep", "Checked out in a worktree"
        elif repo.remote is None:
            status, reason = "keep", "Repository has no remote"
        elif unpushed is None:
            status, reason = "keep", "Could not verify against remotes"
        elif unpushed > 0:
            if gone:
                status, reason = "review", f"Upstream deleted; {unpushed} commit(s) not on any remote (often a squash merge)"
            else:
                status, reason = "keep", f"{unpushed} unpushed commit(s)"
        elif not fresh:
            status, reason = "review", "Fully on remote at last fetch, but fetch is stale; fetch first"
        else:
            status, reason = "safe", "All commits are on the remote" + (" (upstream deleted)" if gone else "")
        base.update(status=status, reason=reason)
        items.append(base)
    return items


def classify_worktrees(repo: Repo, trees: list[dict]) -> list[dict]:
    items = []
    fresh = repo.fresh()
    for t in trees:
        if t["main"] or t["bare"]:
            continue
        exists = os.path.isdir(t["path"])
        base = {"kind": "worktree", "target": t["path"], "branch": t["branch"], "head": t["head"], "locked": t["locked"], "bytes": 0}
        if t["locked"]:
            base.update(status="keep", reason="Locked worktree")
        elif t["prunable"] or not exists:
            base.update(status="safe", reason="Folder already gone; only stale metadata remains", action="prune")
        else:
            base["bytes"] = dir_size(t["path"])
            d = dirty_state(t["path"])
            unp = repo.unpushed(t["head"]) if t["head"] else None
            if d.get("error"):
                base.update(status="keep", reason="Could not read worktree status")
            elif d["counts"]["total"]:
                base.update(status="keep", reason=f"{d['counts']['total']} uncommitted change(s)")
            elif unp is None:
                base.update(status="keep", reason="Could not verify against remotes")
            elif unp > 0:
                base.update(status="keep", reason=f"{unp} unpushed commit(s)")
            elif not fresh:
                base.update(status="review", reason="Clean and pushed at last fetch; fetch first")
            else:
                base.update(status="safe", reason="Clean and fully pushed")
        items.append(base)
    return items


def ignored_set(repo_path: str, rels: list[str]) -> set[str]:
    if not rels:
        return set()
    data = "\0".join(r.replace("\\", "/") + "/" for r in rels).encode("utf-8") + b"\0"
    code, out, _ = git(repo_path, "check-ignore", "-z", "--stdin", stdin=data)
    if code not in (0, 1):
        return set()
    return {p.rstrip("/") for p in out.split("\0") if p}


def has_tracked(repo_path: str, rel: str) -> bool:
    out = git_ok(repo_path, "ls-files", "-z", "--", rel.replace("\\", "/") + "/")
    return out is None or bool(out.strip("\0"))


def find_junk(repo: Repo) -> tuple[list[dict], int]:
    """Walk the main worktree once: junk candidates plus working-tree size."""
    root = repo.path
    candidates: list[tuple[str, str]] = []
    total = 0
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(longp(current)) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if is_link_like(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name == ".git":
                        continue
                    if os.path.exists(os.path.join(e.path, ".git")):
                        continue  # nested repo or linked worktree: scanned separately
                    rel = os.path.relpath(os.path.join(current, e.name), root)
                    if e.name in JUNK_RULES or (e.name in ("venv",) and os.path.exists(os.path.join(e.path, "pyvenv.cfg"))):
                        candidates.append((rel, e.name))
                        continue
                    if depth < JUNK_DEPTH:
                        stack.append((os.path.join(current, e.name), depth + 1))
                    else:
                        total += dir_size(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    ignored = ignored_set(root, [c[0] for c in candidates])
    items = []
    for rel, name in candidates:
        label, manifests, generic = JUNK_RULES[name]
        full = os.path.join(root, rel)
        parent = Path(full).parent
        if name in (".venv", "venv") and not os.path.exists(os.path.join(full, "pyvenv.cfg")):
            continue
        size = dir_size(full)
        total += size
        base = {"kind": "folder", "target": rel, "label": label, "bytes": size}
        rel_key = rel.replace("\\", "/")
        if has_tracked(root, rel):
            base.update(status="keep", reason="Contains files tracked by git")
        elif rel_key not in ignored:
            base.update(status="review", reason="Not git-ignored; confirm it is generated")
        elif generic:
            base.update(status="review", reason="Generic build folder; confirm it can be rebuilt")
        elif not has_manifest(parent, manifests):
            base.update(status="review", reason="Rebuild manifest not found next to it")
        else:
            base.update(status="safe", reason="Ignored, untracked and rebuildable")
        items.append(base)
    return items, total


def gc_item(repo: Repo) -> dict | None:
    out = git_ok(repo.path, "count-objects", "-v") or ""
    vals = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                pass
    loose = (vals.get("size", 0) + vals.get("size-garbage", 0)) * 1024
    if loose < GC_THRESHOLD:
        return None
    return {"kind": "gc", "target": "git gc", "bytes": loose, "status": "safe",
            "reason": f"{human(loose)} of loose or garbage objects; git gc packs them"}


# ---------------------------------------------------------------- scan

def scan_repo(path: str, do_fetch: bool = False) -> dict:
    started = now()
    try:
        repo = Repo(path)
    except RuntimeError as exc:
        return {"path": path, "name": os.path.basename(path), "error": str(exc)}
    result: dict = {"path": repo.path, "name": repo.name, "remote": repo.remote, "default": repo.default}
    if do_fetch:
        result["fetch_error"] = repo.fetch()
    result["fetched_age_h"] = repo.fetched_age_h()
    result["fresh"] = repo.fresh()
    cur = git_ok(repo.path, "symbolic-ref", "--quiet", "--short", "HEAD")
    result["branch"] = cur.strip() if cur else None
    result["detached"] = cur is None and repo.has_head

    # main vs origin/main
    d, r = repo.default, repo.remote
    state, ahead, behind = "no_main", 0, 0
    if d and repo.ref_exists(f"refs/heads/{d}"):
        if not r or not repo.ref_exists(f"refs/remotes/{r}/{d}"):
            state = "no_remote"
        else:
            out = git_ok(repo.path, "rev-list", "--left-right", "--count", f"refs/heads/{d}...refs/remotes/{r}/{d}") or "0\t0"
            ahead, behind = (int(x) for x in out.split())
            state = "in_sync" if not ahead and not behind else "diverged" if ahead and behind else "ahead" if ahead else "behind"
    result.update(main_state=state, ahead=ahead, behind=behind)
    last = git_ok(repo.path, "log", "-1", "--format=%ct") if repo.has_head else None
    result["last_commit"] = int(last) if last and last.strip() else None
    if r and d and repo.ref_exists(f"refs/remotes/{r}/{d}"):
        rt = git_ok(repo.path, "log", "-1", "--format=%ct", f"refs/remotes/{r}/{d}")
        result["remote_tip"] = int(rt) if rt and rt.strip() else None

    # dirty + diff preview
    dstate = dirty_state(repo.path)
    stats = numstat(repo)
    files = []
    for f in dstate["files"][:DIFF_FILES]:
        add, rem = stats.get(f["path"], (None, None))
        files.append({**f, "added": add, "removed": rem})
    result["dirty"] = {**dstate["counts"], "files": files, "truncated": len(dstate["files"]) > DIFF_FILES}
    if dstate.get("error"):
        result["dirty"]["error"] = dstate["error"]
    stash = git_ok(repo.path, "stash", "list") or ""
    result["stashes"] = len([l for l in stash.splitlines() if l.strip()])

    trees = repo.worktrees()
    items = classify_branches(repo, trees) + classify_worktrees(repo, trees)
    junk, wt_bytes = find_junk(repo)
    items += junk
    g = gc_item(repo)
    if g:
        items.append(g)
    for it in items:
        it["id"] = item_id(it["kind"], repo.path, it["target"])
    result["items"] = items
    result["unpushed_branches"] = [{"name": b["target"], "commits": b["unpushed"]} for b in items
                                   if b["kind"] == "branch" and b.get("unpushed")]
    result["worktrees"] = len([t for t in trees if not t["main"]])
    result["worktree_bytes"] = wt_bytes
    result["git_bytes"] = dir_size(repo.git_dir)
    result["safe_bytes"] = sum(i["bytes"] for i in items if i["status"] == "safe")
    result["review_bytes"] = sum(i["bytes"] for i in items if i["status"] == "review")
    result["attention"] = attention(result)
    result["scan_ms"] = int((now() - started) * 1000)
    return result


def attention(r: dict) -> list[str]:
    notes = []
    if r.get("fetch_error"):
        notes.append(f"Fetch failed: {r['fetch_error']}")
    if r["main_state"] == "diverged":
        notes.append(f"{r['default']} diverged: {r['ahead']} ahead, {r['behind']} behind")
    elif r["main_state"] == "behind":
        notes.append(f"{r['default']} is {r['behind']} behind remote")
    elif r["main_state"] == "ahead":
        notes.append(f"{r['default']} has {r['ahead']} unpushed commit(s)")
    if r["dirty"].get("conflicted"):
        notes.append(f"{r['dirty']['conflicted']} conflicted file(s)")
    if r["dirty"].get("total"):
        notes.append(f"{r['dirty']['total']} uncommitted change(s)")
    others = [b for b in r["unpushed_branches"] if b["name"] != r.get("default")]
    if others:
        notes.append(f"{len(others)} other branch(es) with unpushed work")
    if r["stashes"]:
        notes.append(f"{r['stashes']} stash(es)")
    if r.get("remote") and not r.get("fresh"):
        notes.append("Not fetched in 24 h")
    return notes


def scan_caches() -> list[dict]:
    items = []
    for label, paths, status in CACHE_DIRS:
        for p in paths:
            path = expand(p)
            if path and path.is_dir():
                size = dir_size(path)
                items.append({"kind": "cache", "target": str(path), "label": label, "bytes": size, "status": status,
                              "reason": "Re-downloaded on demand" if status == "safe" else "Rebuild needs network or a prune command",
                              "id": item_id("cache", "", str(path))})
                break
    return items


def cmd_scan(args) -> dict:
    roots = [r for r in (args.roots or "").split(";") if r.strip()] or default_roots()
    repos = [p for p in (args.repo or [])] or discover(roots, args.depth)
    results = []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(8, len(repos)))) as pool:
        futures = {pool.submit(scan_repo, p, args.fetch): p for p in repos}
        for fut in cf.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # one broken repo must not fail the scan
                results.append({"path": futures[fut], "name": os.path.basename(futures[fut]), "error": str(exc)})
    results.sort(key=lambda r: (not r.get("attention"), r["name"].lower()))
    caches = scan_caches() if args.caches else []
    ok = [r for r in results if not r.get("error")]
    return {
        "ok": True, "version": VERSION, "scanned_at": int(now()), "roots": roots, "repos": results, "caches": caches,
        "totals": {
            "repos": len(results),
            "needs_attention": len([r for r in ok if r["attention"]]),
            "safe_bytes": sum(r["safe_bytes"] for r in ok) + sum(c["bytes"] for c in caches if c["status"] == "safe"),
            "review_bytes": sum(r["review_bytes"] for r in ok) + sum(c["bytes"] for c in caches if c["status"] == "review"),
            "worktree_bytes": sum(r["worktree_bytes"] for r in ok),
            "git_bytes": sum(r["git_bytes"] for r in ok),
        },
    }


# ---------------------------------------------------------------- actions

def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]


def cmd_backup(args) -> dict:
    repo = Repo(args.repo)
    dest = backup_root(args.dest) / safe_name(repo.name) / stamp()
    files_dir = dest / "files"
    changed: set[str] = set()
    deleted: list[str] = []
    if repo.has_head:
        out = git_ok(repo.path, "diff", "--name-only", "-z", "HEAD") or ""
        changed.update(p for p in out.split("\0") if p)
    else:
        out = git_ok(repo.path, "ls-files", "-z", "--cached") or ""
        changed.update(p for p in out.split("\0") if p)
    out = git_ok(repo.path, "ls-files", "-z", "--others", "--exclude-standard") or ""
    changed.update(p for p in out.split("\0") if p)
    total = 0
    present = []
    for rel in sorted(changed):
        src = os.path.join(repo.path, rel)
        if os.path.isfile(src):
            total += os.path.getsize(src)
            present.append(rel)
        elif not os.path.exists(src):
            deleted.append(rel)
    limit = int(args.max_gb * 1024 ** 3)
    if total > limit:
        return {"ok": False, "error": f"Dirty files total {human(total)}, above the {args.max_gb:g} GB backup limit. "
                                      f"Raise the limit or commit/ignore large assets first.", "bytes": total}
    dest.mkdir(parents=True, exist_ok=False)
    for rel in present:
        target = files_dir / rel
        os.makedirs(longp(target.parent), exist_ok=True)
        shutil.copy2(longp(os.path.join(repo.path, rel)), longp(target))
    if repo.has_head:
        for name, extra in (("changes.patch", ["HEAD"]), ("staged.patch", ["--cached"])):
            code, raw = git_bytes(repo.path, "diff", "--binary", *extra, timeout=120)
            if code == 0 and raw:
                (dest / name).write_bytes(raw)
    bundle = None
    refs = ["--branches"]
    if repo.ref_exists("refs/stash"):
        refs.append("refs/stash")
    unp = repo.unpushed("--branches") if repo.remote else None
    stash_n = len([l for l in (git_ok(repo.path, "stash", "list") or "").splitlines() if l.strip()])
    if repo.has_head and (unp or stash_n or not repo.remote):
        neg = ["--not", "--remotes"] if repo.remote else []
        code, _, err = git(repo.path, "bundle", "create", str(dest / "unpushed.bundle"), *refs, *neg, timeout=600)
        bundle = {"file": "unpushed.bundle", "ok": code == 0, "error": None if code == 0 else err,
                  "unpushed_commits": unp, "stashes": stash_n}
    head = (git_ok(repo.path, "rev-parse", "HEAD") or "").strip() or None
    manifest = {
        "repo": repo.path, "created": dt.datetime.now().isoformat(timespec="seconds"), "head": head,
        "branch": (git_ok(repo.path, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip() or None,
        "files": present, "deleted": deleted, "bytes": total, "bundle": bundle,
        "restore": [
            "Files: copy files/ over the repository (or apply changes.patch with: git apply --binary changes.patch).",
            "Commits: git fetch <path>/unpushed.bundle '+refs/heads/*:refs/remotes/backup/*' then inspect backup/<branch>.",
        ],
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"ok": True, "backup": str(dest), "files": len(present), "deleted": len(deleted), "bytes": total, "bundle": bundle}


def cmd_sync(args) -> dict:
    repo = Repo(args.repo)
    if not repo.remote or not repo.default:
        return {"ok": False, "error": "No remote or default branch to sync with."}
    err = repo.fetch()
    if err:
        return {"ok": False, "error": f"Fetch failed: {err}"}
    d, r = repo.default, repo.remote
    if not repo.ref_exists(f"refs/remotes/{r}/{d}") or not repo.ref_exists(f"refs/heads/{d}"):
        return {"ok": False, "error": f"{d} or {r}/{d} does not exist."}
    out = git_ok(repo.path, "rev-list", "--left-right", "--count", f"refs/heads/{d}...refs/remotes/{r}/{d}") or "0\t0"
    ahead, behind = (int(x) for x in out.split())
    steps = []
    if ahead and behind:
        return {"ok": False, "state": "diverged", "ahead": ahead, "behind": behind,
                "error": f"{d} has diverged ({ahead} ahead, {behind} behind). Repo Pulse does not merge or rebase."}
    if behind:
        holder = next((t for t in repo.worktrees() if t.get("branch") == d and not t["bare"]), None)
        if holder:
            st = dirty_state(holder["path"])
            c = st["counts"]
            if c.get("staged") or c.get("modified") or c.get("conflicted"):
                return {"ok": False, "error": f"{d} is checked out with uncommitted changes in {holder['path']}. Back up or commit first."}
            code, _, e = git(holder["path"], "merge", "--ff-only", "--quiet", f"refs/remotes/{r}/{d}", timeout=120)
        else:
            code, _, e = git(repo.path, "fetch", ".", f"refs/remotes/{r}/{d}:refs/heads/{d}", timeout=120)
        if code != 0:
            return {"ok": False, "error": f"Fast-forward refused: {e}"}
        steps.append(f"Fast-forwarded {d} by {behind} commit(s)")
    if ahead:
        if not args.push:
            steps.append(f"{d} has {ahead} unpushed commit(s); not pushed (push not requested)")
        else:
            code, _, e = git(repo.path, "push", "--quiet", r, f"refs/heads/{d}:refs/heads/{d}", timeout=180)
            if code != 0:
                return {"ok": False, "error": f"Push failed: {e}", "steps": steps}
            steps.append(f"Pushed {ahead} commit(s) to {r}/{d}")
    if not steps:
        steps.append(f"{d} already matches {r}/{d}")
    return {"ok": True, "steps": steps}


def recycle(path: str) -> None:
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
        op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                             FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0 or op.fAnyOperationsAborted:
            raise OSError(f"Recycle Bin refused (code {rc}); paths may be too long. Use permanent delete for rebuildable folders.")
        if os.path.exists(path):
            raise OSError("Recycle Bin did not remove the folder.")
    else:
        trash = backup_root(os.environ.get("REPOPULSE_TRASH")) / "Trash" / stamp()
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(path, str(trash / safe_name(os.path.basename(path))))


def remove_permanently(path: str) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(longp(path), onerror=onerror)


def within(child: str, parent: str) -> bool:
    c, p = os.path.normcase(os.path.realpath(child)), os.path.normcase(os.path.realpath(parent))
    return c != p and os.path.commonpath([c, p]) == p


def cmd_clean(args) -> dict:
    ids = [i for i in (args.ids or "").split(",") if i]
    if not ids:
        return {"ok": False, "error": "No item ids given."}
    if args.repo:
        fresh_scan = scan_repo(args.repo)
        if fresh_scan.get("error"):
            return {"ok": False, "error": fresh_scan["error"]}
        current = {i["id"]: i for i in fresh_scan["items"]}
        repo = Repo(args.repo)
    else:
        current = {i["id"]: i for i in scan_caches()}
        repo = None
    dest = backup_root(args.dest) / (safe_name(repo.name) if repo else "_caches") / f"cleanup-{stamp()}"
    results = []
    for iid in ids:
        it = current.get(iid)
        if not it:
            results.append({"id": iid, "done": False, "reason": "Changed since the scan; rescan first"})
            continue
        allowed = it["status"] == "safe" or (it["status"] == "review" and args.allow_review)
        if not allowed:
            results.append({"id": iid, "target": it["target"], "done": False, "reason": f"Status is {it['status']}: {it['reason']}"})
            continue
        try:
            note = execute(repo, it, dest, args.permanent)
            results.append({"id": iid, "target": it["target"], "kind": it["kind"], "done": True, "bytes": it["bytes"], "note": note})
        except Exception as exc:
            results.append({"id": iid, "target": it["target"], "kind": it["kind"], "done": False, "reason": str(exc)})
    freed = sum(r.get("bytes", 0) for r in results if r["done"])
    return {"ok": True, "results": results, "freed_bytes": freed, "backup": str(dest) if dest.exists() else None}


def execute(repo: Repo | None, it: dict, dest: Path, permanent: bool) -> str:
    kind = it["kind"]
    if kind == "branch":
        dest.mkdir(parents=True, exist_ok=True)
        bundle = dest / f"branch-{safe_name(it['target'])}.bundle"
        code, _, err = git(repo.path, "bundle", "create", str(bundle), f"refs/heads/{it['target']}", timeout=600)
        if code != 0:
            raise RuntimeError(f"Bundle failed, branch kept: {err}")
        code, _, err = git(repo.path, "bundle", "verify", "--quiet", str(bundle), timeout=600)
        if code != 0:
            raise RuntimeError(f"Bundle did not verify, branch kept: {err}")
        code, _, err = git(repo.path, "branch", "-D", it["target"])
        if code != 0:
            raise RuntimeError(err)
        return f"Saved to {bundle.name}; restore with: git fetch \"{bundle}\" {it['target']}:{it['target']}"
    if kind == "worktree":
        if it.get("action") == "prune":
            code, _, err = git(repo.path, "worktree", "prune")
            if code != 0:
                raise RuntimeError(err)
            return "Pruned stale worktree metadata"
        code, _, err = git(repo.path, "worktree", "remove", it["target"], timeout=600)  # no --force: git refuses dirty trees
        if code != 0:
            raise RuntimeError(err)
        return "Worktree removed (its branch is kept)"
    if kind == "folder":
        full = os.path.join(repo.path, it["target"])
        if not within(full, repo.path):
            raise RuntimeError("Path resolves outside the repository; skipped")
        if has_tracked(repo.path, it["target"]):
            raise RuntimeError("Contains tracked files; skipped")
        if permanent:
            if it["status"] != "safe":
                raise RuntimeError("Permanent delete is only allowed for Safe rebuildable folders")
            remove_permanently(full)
            return "Deleted permanently (rebuildable)"
        recycle(full)
        return "Moved to Recycle Bin"
    if kind == "cache":
        if permanent and it["status"] == "safe":
            remove_permanently(it["target"])
            return "Deleted permanently (re-downloadable)"
        recycle(it["target"])
        return "Moved to Recycle Bin"
    if kind == "gc":
        code, _, err = git(repo.path, "gc", "--quiet", timeout=1800)
        if code != 0:
            raise RuntimeError(err)
        return "git gc completed"
    raise RuntimeError(f"Unknown item kind {kind}")


def cmd_diff(args) -> dict:
    repo = Repo(args.repo)
    rel = args.path
    full = os.path.join(repo.path, rel)
    if not within(full, repo.path):
        return {"ok": False, "error": "Path is outside the repository."}
    tracked = git_ok(repo.path, "ls-files", "--error-unmatch", "--", rel) is not None
    if tracked and repo.has_head:
        _, out, _ = git(repo.path, "diff", "HEAD", "--", rel)
    elif os.path.isfile(full):
        with open(full, "rb") as fh:
            raw = fh.read(DIFF_TEXT_LIMIT + 1)
        out = "(binary file)" if b"\0" in raw[:8000] else "New file\n" + "\n".join("+" + l for l in raw.decode("utf-8", "replace").splitlines())
    else:
        out = "(file not found)"
    return {"ok": True, "path": rel, "text": out[:DIFF_TEXT_LIMIT], "truncated": len(out) > DIFF_TEXT_LIMIT}


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repopulse", description="Repo Pulse core")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="Read-only scan")
    s.add_argument("--roots", help="Semicolon-separated folders to search")
    s.add_argument("--repo", action="append", help="Scan only this repo (repeatable)")
    s.add_argument("--depth", type=int, default=MAX_DEPTH)
    s.add_argument("--fetch", action="store_true", help="git fetch --prune first (updates remote-tracking refs only)")
    s.add_argument("--caches", action="store_true", help="Include global package caches")
    b = sub.add_parser("backup", help="Copy dirty files and bundle unpushed commits")
    b.add_argument("--repo", required=True)
    b.add_argument("--dest")
    b.add_argument("--max-gb", type=float, default=2.0)
    y = sub.add_parser("sync", help="Fast-forward default branch; optional non-force push")
    y.add_argument("--repo", required=True)
    y.add_argument("--push", action="store_true")
    c = sub.add_parser("clean", help="Remove scanned items after re-verification")
    c.add_argument("--repo")
    c.add_argument("--ids", required=True)
    c.add_argument("--allow-review", action="store_true")
    c.add_argument("--permanent", action="store_true", help="Delete Safe rebuildable folders instead of recycling")
    c.add_argument("--dest")
    f = sub.add_parser("diff", help="Diff text for one file")
    f.add_argument("--repo", required=True)
    f.add_argument("--path", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"scan": cmd_scan, "backup": cmd_backup, "sync": cmd_sync, "clean": cmd_clean, "diff": cmd_diff}
    try:
        result = handlers[args.cmd](args)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    data = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
