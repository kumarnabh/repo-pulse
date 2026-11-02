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
import contextvars
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "0.5.0"
SCHEMA_VERSION = "1.0"
IS_WIN = os.name == "nt"
FRESH_HOURS = 24
FETCH_CLOCK_SKEW_SECONDS = 60  # tolerate filesystem/tool clock rounding, not arbitrary future evidence
ACTIVE_MINUTES = float(os.environ.get("REPOPULSE_ACTIVE_MINUTES") or 30)  # 0 disables the in-use check
GC_THRESHOLD = 50 * 1024 * 1024
DEFAULT_NAMES = ("main", "master", "trunk", "develop")
LONG_LIVED = re.compile(r"^(main|master|trunk|develop|development|dev|staging|production|prod|release|releases?/.+|hotfix/.+)$")
BUSY_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply", "index.lock")
MAX_DEPTH = 6
JUNK_DEPTH = 6
DIFF_FILES = 60
DIFF_TEXT_LIMIT = 200_000

SKIP_WALK = {
    "node_modules", ".venv", "venv", "__pycache__", ".git", "$recycle.bin",
    "system volume information", "windows", "windows.old", "program files", "program files (x86)",
    "programdata", "appdata", "perflogs", "recovery", "msocache", "windowsapps", "xboxgames",
    "steamapps", ".cache", ".npm", ".nuget", ".gradle", ".cargo", ".rustup", ".pyenv",
    "anaconda3", "miniconda3", "site-packages", "dist-packages", ".vscode", ".vscode-server",
    ".cursor", "onedrivetemp",
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


# core.longpaths: without it git for Windows silently skips paths over 260 characters (status, backup, diff)
GIT_OPTS = ["-c", "core.quotepath=off", "-c", "color.ui=never"] + (["-c", "core.longpaths=true"] if IS_WIN else [])


def git(repo: str | Path, *args: str, timeout: float = 60, stdin: bytes | None = None,
        extra_env: dict | None = None) -> tuple[int, str, str]:
    cmd = ["git", "-C", str(repo), *GIT_OPTS, *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               GCM_INTERACTIVE="never", LC_ALL="C", **(extra_env or {}))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {int(timeout)}s"
    except FileNotFoundError:
        raise RuntimeError("Git is not installed or not on PATH.")
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace").strip()


def git_bytes(repo: str | Path, *args: str, timeout: float = 60) -> tuple[int, bytes]:
    cmd = ["git", "-C", str(repo), *GIT_OPTS, *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GCM_INTERACTIVE="never")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        return 124, b""
    return r.returncode, r.stdout


def git_ok(repo, *args, timeout=60) -> str | None:
    code, out, _ = git(repo, *args, timeout=timeout)
    return out if code == 0 else None


SIZE_CONTEXT = contextvars.ContextVar("size_context", default=None)


def dir_size(path: str | Path, limit_depth: int = 64) -> int:
    context = SIZE_CONTEXT.get()
    key = _norm_key(str(path))
    cached = context["previous"].get(key) if context and limit_depth == 64 else None
    if cached and context["max_age"] > 0 and 0 <= now() - cached["measured_at"] <= context["max_age"]:
        context["measurements"][key] = {**cached, "reused": True}
        return cached["bytes"]
    total = 0
    complete = True
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
                        complete = False
                        continue
        except OSError:
            complete = False
            continue
    if context and limit_depth == 64:
        context["measurements"][key] = {"bytes": total, "measured_at": now(), "reused": False, "complete": complete}
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


def fixed_drives() -> list[str]:
    if not IS_WIN:
        return []
    import ctypes
    import string
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    drives = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask >> i & 1:
            root = f"{letter}:\\"
            if k32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED: skips network, removable and optical drives
                drives.append(root)
    return drives


def default_roots() -> list[str]:
    """Every fixed drive on Windows (system folders are skipped while walking); the home folder elsewhere."""
    return fixed_drives() or [str(Path.home())]


def backup_root(dest: str | None) -> Path:
    return Path(dest) if dest else Path.home() / "RepoPulse Backups"


def path_tag(path: str) -> str:
    """Short stable tag for a path, so two repos named `app` never share a backup folder."""
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:8]


def unique_dir(parent: Path, base: str) -> Path:
    """Create and return parent/base, or parent/base-N when another run already took that name."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1000):
        d = parent / (base if n == 0 else f"{base}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not create a unique folder under {parent}")


def unique_file(folder: Path, stem: str, ext: str) -> Path:
    for n in range(1000):
        p = folder / (f"{stem}{ext}" if n == 0 else f"{stem}-{n}{ext}")
        if not os.path.lexists(p):
            return p
    raise RuntimeError(f"Could not pick a unique file name in {folder}")


def free_bytes(path: Path) -> int | None:
    p = Path(os.path.abspath(path))
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(longp(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ENCRYPT_LIMIT = 512 * 1024 * 1024
ENC_SUFFIX = ".dpapi"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI, scoped to the current user: only this Windows account on this machine can decrypt."""
    if not IS_WIN:
        raise OSError("Encrypted backups use Windows DPAPI and are only available on Windows")
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    inp, _keep1 = blob(data)
    ent, _keep2 = blob(b"RepoPulse backup v1")
    out = BLOB()
    if protect:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(BLOB), wintypes.LPCWSTR, ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p,
                       wintypes.DWORD, ctypes.POINTER(BLOB)]
        ok = fn(ctypes.byref(inp), "Repo Pulse backup", ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p,
                       wintypes.DWORD, ctypes.POINTER(BLOB)]
        ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the data (DPAPI error "
                      f"{ctypes.GetLastError()}); it can only be decrypted by the same Windows account")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def encrypt_bytes(data: bytes) -> bytes:
    return _dpapi(data, True)


def decrypt_bytes(data: bytes) -> bytes:
    return _dpapi(data, False)


def read_backup_file(path: Path) -> bytes:
    """Plain content of a backed-up file, decrypting a `.dpapi` copy when that is what exists."""
    if not os.path.exists(longp(path)) and os.path.exists(longp(str(path) + ENC_SUFFIX)):
        with open(longp(str(path) + ENC_SUFFIX), "rb") as fh:
            return decrypt_bytes(fh.read())
    with open(longp(path), "rb") as fh:
        return fh.read()


def copy_verified(src: str, dst: Path, encrypt: bool = False) -> str:
    """Copy one regular file and prove the copy: the copy must hash equal to the source, and the source
    must not have changed while it was read. Raises OSError (the copy is removed) otherwise.

    encrypt=True writes `<dst>.dpapi` instead, and proves it by decrypting it again.
    """
    os.makedirs(longp(dst.parent), exist_ok=True)
    if encrypt:
        return _copy_encrypted(src, Path(str(dst) + ENC_SUFFIX))
    h = hashlib.sha256()
    try:
        with open(longp(src), "rb") as fi, open(longp(dst), "xb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(longp(src), longp(dst))
        digest = h.hexdigest()
        if file_sha256(dst) != digest:
            raise OSError("the copy does not match the source")
        if file_sha256(src) != digest:
            raise OSError("the file changed while it was being copied")
        return digest
    except OSError:
        try:
            os.chmod(longp(dst), stat.S_IWRITE)
            os.remove(longp(dst))
        except OSError:
            pass
        raise


def _copy_encrypted(src: str, dst: Path) -> str:
    try:
        if os.path.getsize(longp(src)) > ENCRYPT_LIMIT:
            raise OSError(f"larger than {human(ENCRYPT_LIMIT)}; too large to encrypt")
        with open(longp(src), "rb") as fh:
            data = fh.read()
        digest = hashlib.sha256(data).hexdigest()
        with open(longp(dst), "xb") as fo:
            fo.write(encrypt_bytes(data))
            fo.flush()
            os.fsync(fo.fileno())
        with open(longp(dst), "rb") as fh:
            if hashlib.sha256(decrypt_bytes(fh.read())).hexdigest() != digest:
                raise OSError("the encrypted copy does not decrypt to the source")
        if file_sha256(src) != digest:
            raise OSError("the file changed while it was being copied")
        return digest
    except OSError:
        try:
            os.remove(longp(dst))
        except OSError:
            pass
        raise


def copy_tree_verified(root: str, rel: str, dest_root: Path, encrypt: bool = False) -> tuple[dict[str, str], list[dict]]:
    """copy_verified for a file or folder under root. Links are never followed: they are recorded."""
    hashes: dict[str, str] = {}
    problems: list[dict] = []
    stack = [rel.rstrip("/\\")]
    while stack:
        cur = stack.pop()
        full = os.path.join(root, cur)
        try:
            st = os.lstat(longp(full))
        except OSError as exc:
            problems.append({"path": cur, "error": exc.strerror or str(exc)})
            continue
        if stat.S_ISLNK(st.st_mode) or is_junction(full):
            problems.append({"path": cur, "error": "link not followed", "link": True})
        elif stat.S_ISDIR(st.st_mode):
            try:
                stack += [os.path.join(cur, n) for n in os.listdir(longp(full))]
            except OSError as exc:
                problems.append({"path": cur, "error": exc.strerror or str(exc)})
        elif stat.S_ISREG(st.st_mode):
            try:
                hashes[cur.replace("\\", "/")] = copy_verified(full, dest_root / cur, encrypt)
            except OSError as exc:
                problems.append({"path": cur, "error": exc.strerror or str(exc)})
    return hashes, problems


def is_junction(path: str) -> bool:
    fn = getattr(os.path, "isjunction", None)  # Python 3.12+
    if fn:
        return fn(path)
    if not IS_WIN:
        return False
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400) and not os.path.islink(path)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def new_op_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def log_operation(dest: str | None, record: dict) -> None:
    """Append one line to the operation journal next to the backups. Never fails the action itself.

    Destructive actions write a `started` line before they change anything and a `finished` line after,
    so `recover` can find actions cut short by a crash, power loss or forced termination.
    """
    try:
        root = backup_root(dest)
        root.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "version": VERSION, **record}, ensure_ascii=False)
        with open(root / "operations.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def read_operations(dest: str | None) -> list[dict]:
    path = backup_root(dest) / "operations.jsonl"
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(rec)
                except ValueError:
                    continue  # a line cut short by a crash
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- discovery

def git_kind(folder: str) -> str | None:
    """'repo' for a main worktree, 'worktree' for a linked worktree, None otherwise (incl. submodules)."""
    dotgit = os.path.join(folder, ".git")
    if os.path.isdir(dotgit):
        return "repo"
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding="utf-8", errors="replace") as fh:
                target = fh.read(4096).strip().replace("\\", "/")
        except OSError:
            return None
        if target.startswith("gitdir:") and "/worktrees/" in target:
            return "worktree"
        if target.startswith("gitdir:") and "/modules/" not in target:
            return "repo"  # separate git directory; Repo validates the actual top-level path
    if os.path.isfile(os.path.join(folder, "HEAD")) and os.path.isdir(os.path.join(folder, "objects")):
        if (git_ok(folder, "rev-parse", "--is-bare-repository") or "").strip() == "true":
            return "repo"
    return None


def hint_paths() -> list[tuple[str, str]]:
    """Folders other tools already know about: Claude Code, Codex, VS Code family."""
    hints: list[tuple[str, str]] = []
    home = Path.home()
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        hints += [(p, "Claude Code") for p in (data.get("projects") or {}) if isinstance(p, str)]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
        for quote, key in re.findall(r'^\s*\[projects\.(["\'])(.+?)\1\]', text, re.M):
            hints.append((key.replace("\\\\", "\\") if quote == '"' else key, "Codex"))
    except OSError:
        pass
    base = os.environ.get("APPDATA")
    for app in ("Code", "Cursor", "Windsurf", "Code - Insiders"):
        storage = (Path(base, app) if base else home / ".config" / app) / "User" / "globalStorage"
        uris: list[str] = []
        try:
            uris += _file_uris(json.loads((storage / "storage.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        uris += _vscode_recent(storage / "state.vscdb")
        for uri in uris:
            path = urllib.parse.unquote(uri[len("file://"):])
            if IS_WIN and re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            hints.append((path, app))
    jb = Path(base, "JetBrains") if base else home / ".config" / "JetBrains"
    for xml in sorted(jb.glob("*/options/recentProjects.xml")) if jb.is_dir() else []:
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key in re.findall(r'<entry key="([^"]+)"', text):
            hints.append((key.replace("$USER_HOME$", str(home)), "JetBrains"))
    return hints


def _vscode_recent(db: Path) -> list[str]:
    """Recently opened folders from VS Code's state database (read-only, never locks it)."""
    if not db.is_file():
        return []
    import sqlite3
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True, timeout=1)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key = 'history.recentlyOpenedPathsList'").fetchone()
        finally:
            con.close()
        return _file_uris(json.loads(row[0])) if row else []
    except (sqlite3.Error, ValueError, TypeError):
        return []


def _file_uris(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _file_uris(k) + _file_uris(v)
    elif isinstance(node, list):
        for v in node:
            out += _file_uris(v)
    elif isinstance(node, str) and node.startswith("file:///"):
        out.append(node)
    return out


def enclosing_git(path: str) -> tuple[str, str] | None:
    p = os.path.abspath(os.path.expanduser(path))
    if "$" in path or not os.path.exists(p):  # deleted project or unexpanded IDE macro: never guess a parent
        return None
    for _ in range(12):
        if os.path.isdir(p):
            kind = git_kind(p)
            if kind:
                return p, kind
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    return None


def linked_worktrees(repo_path: str) -> list[str]:
    out = git_ok(repo_path, "worktree", "list", "--porcelain", "-z")
    if out is None:
        return []
    return [os.path.normpath(t["path"]) for t in parse_worktrees(out)[1:]
            if os.path.isdir(t["path"]) and git_kind(t["path"]) == "worktree"]


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def is_excluded(path: str, excluded: list[str]) -> bool:
    if not excluded:
        return False
    key = _norm_key(path)
    return any(key == e or key.startswith(e.rstrip("\\/") + os.sep) for e in excluded)


def contains_excluded(path: str, excluded: list[str]) -> bool:
    if not excluded:
        return False
    key = _norm_key(path).rstrip("\\/") + os.sep
    return any(e.startswith(key) for e in excluded)


def discover(roots: list[str], max_depth: int = MAX_DEPTH, hints: bool = True, exclude: list[str] | None = None,
             diagnostics: list[dict] | None = None) -> list[dict]:
    """Main repos and linked worktrees under the roots, from tool history, and from each repo's worktree list."""
    found: dict[str, dict] = {}
    excluded = [_norm_key(e) for e in exclude or [] if e.strip()]

    def add(path: str, kind: str, source: str) -> None:
        key = _norm_key(path)
        if key not in found and not is_excluded(path, excluded):
            found[key] = {"path": os.path.normpath(path), "kind": kind, "source": source}

    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if is_excluded(root, excluded):
            continue
        if not os.path.isdir(root):
            if diagnostics is not None:
                diagnostics.append({"path": root, "code": "root_unavailable", "message": "Scan root is missing or inaccessible"})
            continue
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            kind = git_kind(current)
            if kind:
                add(current, kind, "folders")
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
                        if excluded and is_excluded(e.path, excluded):
                            continue
                        stack.append((e.path, depth + 1))
            except OSError as exc:
                if diagnostics is not None:
                    diagnostics.append({"path": current, "code": "discovery_unreadable", "message": str(exc)})
                continue
    if hints:
        for path, source in hint_paths():
            hit = enclosing_git(path)
            if hit:
                add(hit[0], hit[1], source)
    # A linked worktree discovered alone must bring its owning worktree into the inventory.
    for entry in list(found.values()):
        if entry["kind"] == "worktree":
            out = git_ok(entry["path"], "worktree", "list", "--porcelain", "-z")
            trees = parse_worktrees(out) if out is not None else []
            if trees and not trees[0]["bare"]:
                add(trees[0]["path"], "repo", "worktree owner")
    for entry in [e for e in found.values() if e["kind"] == "repo"]:
        for wt in linked_worktrees(entry["path"]):
            add(wt, "worktree", "worktree list")
    return sorted(found.values(), key=lambda e: e["path"].lower())


# ---------------------------------------------------------------- repo facts

def parse_worktrees(text: str) -> list[dict]:
    """Porcelain -z keeps embedded newlines, tabs and quotes in paths/reasons literal."""
    trees = []
    for block in text.split("\0\0"):
        cur = None
        for field in block.split("\0"):
            key, _, value = field.partition(" ")
            if key == "worktree":
                cur = {"path": os.path.normpath(value), "head": None, "branch": None,
                       "locked": False, "lock_reason": None, "prunable": False,
                       "prune_reason": None, "bare": False, "main": not trees}
                trees.append(cur)
            elif cur is not None:
                if key == "HEAD":
                    cur["head"] = None if value and set(value) == {"0"} else value
                elif key == "branch":
                    cur["branch"] = value.removeprefix("refs/heads/")
                elif key in ("bare", "locked", "prunable"):
                    cur[key] = True
                    if key in ("locked", "prunable"):
                        cur["lock_reason" if key == "locked" else "prune_reason"] = value or None
    return trees


class Repo:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path.rstrip("\\/")) or self.path
        self.bare = (git_ok(self.path, "rev-parse", "--is-bare-repository") or "").strip() == "true"
        top = git_ok(self.path, "rev-parse", "--show-toplevel") if not self.bare else None
        if not self.bare and (top is None or _norm_key(top.strip()) != _norm_key(self.path)):
            raise RuntimeError("Not a readable repository root; Git resolved a parent or the metadata is invalid.")
        common = git_ok(self.path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is None:
            raise RuntimeError("Not a readable git repository (check safe.directory or permissions).")
        self.git_dir = common.strip()
        if self.bare and _norm_key(self.git_dir) != _norm_key(self.path):
            raise RuntimeError("Not a bare repository root; Git resolved a parent repository.")
        remote_out = git_ok(self.path, "remote")
        self.read_errors = [] if remote_out is not None else ["Could not list remotes"]
        remotes = (remote_out or "").splitlines()
        self.remotes = remotes
        self.remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        self.default = self._default_branch()
        self.has_head = git_ok(self.path, "rev-parse", "--verify", "-q", "HEAD") is not None
        self._unpushed_commits: set[str] | bool | None = None
        self._features: dict | None = None

    def features(self) -> dict:
        if self._features is None:
            self._features = repo_features(self)
        return self._features

    def _default_branch(self) -> str | None:
        if self.remote:
            sym = git_ok(self.path, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{self.remote}/HEAD")
            if sym and "/" in sym.strip():
                return sym.strip().split("/", 1)[1]
        configured = (git_ok(self.path, "config", "--get", "init.defaultBranch") or "").strip()
        names = ([configured] if configured else []) + [b for b in DEFAULT_NAMES if b != configured]
        if self.remote:
            for b in names:
                if self.ref_exists(f"refs/remotes/{self.remote}/{b}"):
                    return b
        for b in names:
            if self.ref_exists(f"refs/heads/{b}"):
                return b
        cur = git_ok(self.path, "symbolic-ref", "--quiet", "--short", "HEAD")
        return cur.strip() if cur else None

    def ref_exists(self, ref: str) -> bool:
        return git_ok(self.path, "rev-parse", "--verify", "-q", ref) is not None

    def _fetch_state_path(self) -> str:
        return os.path.join(self.git_dir, "repopulse-fetch.json")

    def fetch_state(self) -> dict:
        try:
            with open(self._fetch_state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _record_fetch(self, ok: bool) -> None:
        state = self.fetch_state()
        entry = state.get(self.remote) if isinstance(state.get(self.remote), dict) else {}
        entry["ok" if ok else "failed"] = now()
        state[self.remote] = entry
        try:
            write_json_atomic(Path(self._fetch_state_path()), state)
        except OSError:
            pass

    def last_fetch(self, remote: str | None = None) -> float | None:
        """Time of the last fetch of this repo's remote known to have succeeded.

        FETCH_HEAD is shared by every remote and is rewritten even by a fetch that fails part-way, so a
        fetch that Repo Pulse saw fail only counts as fresh again after a later successful fetch, or after
        FETCH_HEAD is rewritten by another tool after that failure.
        """
        entry = self.fetch_state().get(remote or self.remote) or {}
        if not isinstance(entry, dict):
            return None
        ok_t, fail_t = entry.get("ok"), entry.get("failed")
        times = [ok_t] if isinstance(ok_t, (int, float)) and (not isinstance(fail_t, (int, float)) or ok_t > fail_t) else []
        try:
            fh = os.path.getmtime(os.path.join(self.git_dir, "FETCH_HEAD"))
            if len(self.remotes) == 1 and (not isinstance(fail_t, (int, float)) or fh > fail_t):
                times.append(fh)
        except OSError:
            pass
        return max(times) if times else None

    def fetched_age_h(self) -> float | None:
        t = self.last_fetch()
        return None if t is None else round((now() - t) / 3600, 2)

    def fresh(self) -> bool:
        fetched = self.last_fetch()
        return fetched is not None and -FETCH_CLOCK_SKEW_SECONDS <= now() - fetched <= FRESH_HOURS * 3600

    def admin_dirs(self) -> list[str]:
        """The common git dir plus every linked worktree's private git dir."""
        dirs = [self.git_dir]
        root = os.path.join(self.git_dir, "worktrees")
        try:
            dirs += [os.path.join(root, n) for n in os.listdir(root)]
        except OSError:
            pass
        return dirs

    def busy(self) -> list[str]:
        """Git operations in progress anywhere in this repository (merge, rebase, bisect, a held index lock)."""
        found = []
        for d in self.admin_dirs():
            for m in BUSY_MARKERS:
                if os.path.exists(os.path.join(d, m)):
                    found.append(f"{m} in {os.path.basename(d) if d != self.git_dir else 'main worktree'}")
        for m in ("gc.pid", "packed-refs.lock", "shallow.lock"):
            if os.path.exists(os.path.join(self.git_dir, m)):
                found.append(m)
        return found

    def rebasing_branches(self) -> set[str]:
        names = set()
        for d in self.admin_dirs():
            for sub in ("rebase-merge", "rebase-apply"):
                try:
                    with open(os.path.join(d, sub, "head-name"), encoding="utf-8") as fh:
                        names.add(fh.read().strip().removeprefix("refs/heads/"))
                except OSError:
                    pass
        return names

    def integrated(self, rev: str) -> bool | None:
        """True when rev is already part of the remote default branch (merged), not just pushed somewhere."""
        if not self.remote or not self.default or not self.ref_exists(f"refs/remotes/{self.remote}/{self.default}"):
            return None
        code, _, _ = git(self.path, "merge-base", "--is-ancestor", rev, f"refs/remotes/{self.remote}/{self.default}")
        return True if code == 0 else False if code == 1 else None

    def unpushed(self, rev: str) -> int | None:
        if not self.remote:
            return None
        out = git_ok(self.path, "rev-list", "--count", rev, "--not", "--remotes")
        return int(out.strip()) if out else None

    def unpushed_tip(self, name: str, sha: str) -> int | None:
        """unpushed() for a local branch, skipping the git call when the tip is already on a remote.

        One `rev-list --branches --not --remotes` lists every commit that is on no remote; a branch
        whose tip is not in it is fully pushed. Repos with hundreds of branches need one call, not hundreds.
        """
        if not self.remote:
            return None
        if self._unpushed_commits is None:
            out = git_ok(self.path, "rev-list", "--branches", "--not", "--remotes")
            self._unpushed_commits = set(out.split()) if out is not None else False
        if self._unpushed_commits is not False and sha and sha not in self._unpushed_commits:
            return 0
        return self.unpushed(f"refs/heads/{name}")

    def fetch(self, timeout: float = 45) -> str | None:
        """None on success or when there is nothing to fetch from; otherwise the error."""
        if not self.remote:
            return None
        code, _, err = git(self.path, "fetch", "--prune", "--no-tags", "--quiet", self.remote, timeout=timeout)
        self._record_fetch(code == 0)
        return None if code == 0 else (err.splitlines()[-1] if err else f"fetch failed ({code})")

    def worktrees(self) -> list[dict] | None:
        """Parsed `git worktree list`; None when git could not list them."""
        out = git_ok(self.path, "worktree", "list", "--porcelain", "-z")
        if out is None:
            return None
        return parse_worktrees(out)


def repo_features(repo: Repo) -> dict:
    """Layouts that change what is safe: shallow and partial clones, sparse checkout, Git LFS, submodules."""
    code, cfg, _ = git(repo.path, "config", "--get-regexp",
                       r"^(remote\..*\.promisor|extensions\.partialclone|core\.sparsecheckout)$")
    config_known = code in (0, 1)
    cfg = cfg.lower()
    shallow = git_ok(repo.path, "rev-parse", "--is-shallow-repository")
    if not config_known or shallow is None:
        repo.read_errors.append("Could not determine clone features")
    try:
        attrs = Path(repo.path, ".gitattributes").read_text(encoding="utf-8", errors="replace")
    except OSError:
        attrs = ""
    return {
        "shallow": shallow.strip() == "true" if shallow is not None else None,
        "partial_clone": ("promisor true" in cfg or "extensions.partialclone" in cfg) if config_known else None,
        "sparse": "core.sparsecheckout true" in cfg,
        "lfs": "filter=lfs" in attrs or os.path.isdir(os.path.join(repo.git_dir, "lfs")),
        "submodules": os.path.isfile(os.path.join(repo.path, ".gitmodules")),
    }


def remote_health(repo: Repo) -> list[dict]:
    """Broken upstream tracking, remotes without URLs, local remotes whose folder is gone."""
    notes = []
    remotes = set((git_ok(repo.path, "remote") or "").split())
    for line in (git_ok(repo.path, "config", "--get-regexp", r"^branch\..*\.remote$") or "").splitlines():
        key, _, val = line.partition(" ")
        branch = key[len("branch."):-len(".remote")]
        if val and val != "." and val not in remotes:
            notes.append({"level": "warn", "text": f"{branch} tracks remote '{val}', which does not exist"})
    for r in sorted(remotes):
        url = (git_ok(repo.path, "remote", "get-url", r) or "").strip()
        if not url:
            notes.append({"level": "warn", "text": f"Remote {r} has no URL"})
            continue
        local = url[7:] if url.startswith("file://") else url
        if (os.path.isabs(local) or local.startswith(("./", "../"))) and not os.path.exists(os.path.join(repo.path, local)):
            notes.append({"level": "warn", "text": f"Remote {r} points to a folder that no longer exists: {url}"})
    if repo.remote and not repo.ref_exists(f"refs/remotes/{repo.remote}/HEAD"):
        notes.append({"level": "info", "text": f"{repo.remote}/HEAD is not set; run git remote set-head {repo.remote} --auto"})
    return notes


def public_remote_url(url: str) -> str:
    """Remote inventory is useful to clients, embedded credentials are not."""
    if "://" in url:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return url


def remote_inventory(repo: Repo, errors: list[str]) -> list[dict]:
    state = repo.fetch_state()
    rows = []
    for name in sorted(repo.remotes):
        urls = {}
        for mode, flags in (("fetch_urls", []), ("push_urls", ["--push"])):
            out = git_ok(repo.path, "remote", "get-url", *flags, "--all", name)
            if out is None:
                errors.append(f"Could not read {mode} for remote {name}")
            urls[mode] = [public_remote_url(u) for u in (out or "").splitlines()]
        entry = state.get(name) if isinstance(state.get(name), dict) else {}
        last = repo.last_fetch(name)
        age = None if last is None else max(0, round((now() - last) / 3600, 2))
        failed = entry.get("failed")
        failed = failed if isinstance(failed, (int, float)) else None
        recorded = entry.get("ok")
        success = last if last is not None else recorded if isinstance(recorded, (int, float)) else None
        rows.append({"name": name, **urls, "last_success_at": success, "last_failure_at": failed,
                     "fresh": last is not None and -FETCH_CLOCK_SKEW_SECONDS <= now() - last <= FRESH_HOURS * 3600,
                     "fetched_age_h": age, "contact": "failed" if failed and (last is None or failed >= last) else "not_checked"})
    return rows


def agent_origin(path: str) -> str | None:
    path = path.replace("\\", "/").lower()
    if "/.codex/worktrees/" in path:
        return "codex"
    if "/.claude/worktrees/" in path or "/.claude-worktrees/" in path:
        return "claude"
    return None


def worktree_inventory(repo: Repo, trees: list[dict], items: list[dict]) -> list[dict]:
    classified = {_norm_key(i["target"]): i for i in items if i["kind"] == "worktree"}
    rows = []
    for t in trees:
        item = classified.get(_norm_key(t["path"]))
        exists = os.path.isdir(t["path"])
        agent = agent_origin(t["path"])
        orphan = "missing" if not exists else "candidate" if agent and item and item["status"] in ("safe", "review") else "not_proven"
        rows.append({**t, "exists": exists, "agent_origin": agent, "orphan_status": orphan,
                     "stale": bool(t["prunable"] or not exists),
                     "cleanup_status": item["status"] if item else "keep",
                     "reason": item["reason"] if item else "Main worktree or bare repository is retained",
                     "bytes": item.get("bytes") if item else None})
    return rows


def assess_readiness(row: dict) -> None:
    """Advisory only. Action commands must always perform their own checks."""
    findings = []

    def note(code: str, level: str, reason: str, guidance: str) -> None:
        findings.append({"code": code, "level": level, "reason": reason, "guidance": guidance})

    if row.get("error") or row.get("health") == "unknown":
        note("inventory_unknown", "unknown", row.get("error") or "; ".join(row.get("errors") or []),
             "Resolve Git access/read errors and refresh before making decisions.")
    if row.get("operations_in_progress"):
        note("operation_in_progress", "blocked", "; ".join(row["operations_in_progress"]),
             "Inspect git status and Repo Pulse recover. Finish or deliberately recover the operation; do not remove locks blindly.")
    if row.get("dirty", {}).get("conflicted"):
        note("conflicts", "blocked", "Unresolved conflicts", "Preserve a backup and resolve the interrupted Git operation manually.")
    if row.get("dirty", {}).get("total"):
        note("uncommitted_changes", "protected", "Working tree contains uncommitted files", "Resume in this checkout; back up before syncing or cleaning.")
    if row.get("detached"):
        note("detached_head", "protected", "HEAD is detached", "Create a named branch at HEAD before leaving or removing this checkout.")
    if row.get("unpushed_branches") or row.get("branch_ahead"):
        note("unpushed_commits", "protected", "Local commits differ from remote-tracking refs", "Back up or push intentionally; fetch before judging current remote state.")
    if row.get("stashes"):
        note("stashes", "protected", "Repository has shared stash entries", "Inspect git stash list; Repo Pulse backup includes stashes.")
    if not row.get("error") and not row.get("remote") and not row.get("unborn"):
        note("local_only", "protected", "No configured remote protects this history", "Keep a verified backup of this local repository.")
    if row.get("bare"):
        note("bare_repository", "review", "Bare repository has no working checkout", "Create or select a working clone before resuming development.")
    if row.get("remote") and not row.get("fresh"):
        note("remote_stale", "review", "Remote state is stale or a fetch failed", "Run an explicit fetch when online, then refresh. Cached refs do not prove server state.")
    if row.get("main_state") == "diverged" or (row.get("branch_ahead") and row.get("branch_behind")):
        note("diverged", "review", "Local and tracking histories diverged", "Back up and reconcile manually; safe sync will refuse divergence.")
    if row.get("features", {}).get("shallow") or row.get("features", {}).get("partial_clone"):
        note("incomplete_history", "review", "Shallow or partial clone", "Inspect clone limitations; reachability and offline object availability may be incomplete.")
    if any(n["level"] == "warn" for n in row.get("remote_health", [])):
        note("remote_configuration", "review", "Remote configuration needs attention", "Inspect remote_health and correct the remote/upstream before syncing.")
    levels = {f["level"] for f in findings}
    risk = next((level for level in ("unknown", "blocked", "protected", "review") if level in levels), "healthy")
    hard = next((level for level in ("unknown", "blocked") if level in levels), None)
    resume = hard or ("review" if row.get("detached") or row.get("bare") else "safe")
    sync = hard or ("review" if (row.get("dirty", {}).get("total") or not row.get("fresh") or
                                 row.get("main_state") not in ("in_sync", "behind") or row.get("kind") == "worktree" or row.get("bare") or
                                 any(c in {f["code"] for f in findings} for c in ("incomplete_history", "remote_configuration"))) else "safe")
    safe_ids = [i["id"] for i in row.get("items", []) if i["status"] == "safe"] if not hard else []
    row["risk"] = {"level": risk, "findings": findings}
    row["readiness"] = {
        "resume": {"state": resume, "reason": "Inspect the findings before continuing" if resume != "safe" else "Checkout is readable; preserve its existing work"},
        "sync": {"state": sync, "reason": "Default branch can be checked for fast-forward sync" if sync == "safe" else "Review findings; explicit sync fetches and rechecks before changing refs"},
        "clean": {"state": hard or ("safe" if safe_ids else "review"), "safe_item_ids": safe_ids,
                  "reason": "Only listed items are candidates; explicit selection and live revalidation are required"},
    }


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
    # "all" lists each file inside a new folder, so counts, previews and backups see real files
    code, out, err = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=90)
    if code != 0:
        return {**parse_status(""), "error": err or "status failed"}
    return parse_status(out)


def numstat(repo: Repo) -> dict[str, tuple] | None:
    """Line counts per changed path; None when git diff failed."""
    if not repo.has_head:
        return {}
    out = git_ok(repo.path, "diff", "--numstat", "-z", "HEAD")
    if out is None:
        return None
    # -z: "add\trem\tpath\0", or for renames "add\trem\t\0old\0new\0"
    parts = out.split("\0")
    stats = {}
    i = 0
    while i < len(parts):
        bits = parts[i].split("\t", 2)
        i += 1
        if len(bits) != 3:
            continue
        add, rem, p = bits
        if not p and i + 1 < len(parts):
            p = parts[i + 1]
            i += 2
        stats[p] = (None if add == "-" else int(add), None if rem == "-" else int(rem))
    return stats


# ---------------------------------------------------------------- classification

def classify_branches(repo: Repo, trees: list[dict], only: str | None = None) -> list[dict] | None:
    """None when git could not list the branches (the caller reports it instead of showing none)."""
    checked_out = {t["branch"] for t in trees if t.get("branch")}
    rebasing = repo.rebasing_branches()
    fmt = "%(refname)%00%(upstream:short)%00%(upstream:track)%00%(objectname)%00%(committerdate:unix)"
    pattern = f"refs/heads/{only}" if only else "refs/heads"
    out = git_ok(repo.path, "for-each-ref", f"--format={fmt}", pattern)
    if out is None:
        return None
    fresh = repo.fresh()
    items = []
    for line in out.splitlines():
        ref, upstream, track, sha, when = (line.split("\0") + ["", "", "", "", "0"])[:5]
        name = ref.removeprefix("refs/heads/")
        if only and name != only:
            continue
        unpushed = repo.unpushed_tip(name, sha)
        gone = "[gone]" in track
        merged = repo.integrated(sha) if unpushed == 0 else False
        base = {
            "kind": "branch", "target": name, "upstream": upstream or None, "upstream_gone": gone,
            "head": sha, "unpushed": unpushed, "merged": merged, "last_commit": int(when or 0), "bytes": 0,
            "upstream_ahead": int(re.search(r"ahead (\d+)", track)[1]) if "ahead " in track else (0 if upstream and not gone else None),
            "upstream_behind": int(re.search(r"behind (\d+)", track)[1]) if "behind " in track else (0 if upstream and not gone else None),
            "checked_out_paths": [t["path"] for t in trees if t.get("branch") == name],
        }
        if name == repo.default:
            status, reason = "keep", "Default branch"
        elif name in checked_out:
            status, reason = "keep", "Checked out in a worktree"
        elif name in rebasing:
            status, reason = "keep", "Being rebased"
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
        elif LONG_LIVED.match(name):
            status, reason = "review", "Long-lived branch name (release, develop, staging...); confirm it is retired"
        elif repo.features()["shallow"] is not False or repo.features()["partial_clone"] is not False:
            status, reason = "review", "Shallow clone: history is incomplete, so this cannot be fully proven pushed"
        elif merged:
            status, reason = "safe", f"Merged into {repo.remote}/{repo.default}" + (" (upstream deleted)" if gone else "")
        else:
            status, reason = "safe", "All commits are on a remote, but not merged into " \
                                     f"{repo.remote}/{repo.default}" + (" (upstream deleted)" if gone else "")
        base.update(status=status, reason=reason)
        items.append(base)
    return items


def worktree_admin_dir(repo: Repo, path: str) -> str | None:
    """The private git dir (.git/worktrees/<id>) that belongs to the worktree at path."""
    want = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    root = os.path.join(repo.git_dir, "worktrees")
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for n in names:
        d = os.path.join(root, n)
        try:
            with open(os.path.join(d, "gitdir"), encoding="utf-8", errors="replace") as fh:
                gitdir = fh.read().strip()
        except OSError:
            continue
        gitdir = os.path.normpath(os.path.join(d, gitdir))  # relative when worktree.useRelativePaths is set
        if os.path.normcase(os.path.dirname(gitdir)) == want:
            return d
    return None


def recent_activity(path: str, admin: str | None, minutes: float | None = None, budget: int = 50_000) -> float | None:
    """Newest modification time (seconds ago) within the last `minutes`, or None if the worktree is quiet.

    A coding agent or editor working in a worktree writes files and runs git (which refreshes the index),
    so recent writes are the most reliable cross-platform sign that it is still in use.
    """
    cutoff = now() - (ACTIVE_MINUTES if minutes is None else minutes) * 60
    newest = 0.0
    for name in ("index", "HEAD", "logs/HEAD"):
        if admin:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(admin, name)))
            except OSError:
                pass
    stack, seen = [longp(path)], 0
    while stack and seen < budget and newest < cutoff:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    try:
                        if is_link_like(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if e.name != ".git" and e.name not in JUNK_RULES:
                                stack.append(e.path)
                        else:
                            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return round(now() - newest) if newest >= cutoff else None


def classify_worktrees(repo: Repo, trees: list[dict], measure: bool = True, excluded: list[str] | None = None) -> list[dict]:
    """measure=False leaves sizes to the caller (a full scan reuses each worktree row's own walk)."""
    items = []
    fresh = repo.fresh()
    for t in trees:
        if t["main"] or t["bare"]:
            continue
        exists = os.path.isdir(t["path"])
        admin = worktree_admin_dir(repo, t["path"])
        base = {"kind": "worktree", "target": t["path"], "branch": t["branch"], "head": t["head"], "locked": t["locked"], "bytes": 0}
        if contains_excluded(t["path"], excluded or []):
            base.update(status="keep", reason="Contains an excluded folder; retain this worktree")
        elif t["locked"]:
            base.update(status="keep", reason="Locked worktree")
        elif t["prunable"] or not exists:
            orphans: int | None = 0
            if t["head"]:  # also protect a checkout whose branch ref was removed externally
                out = git_ok(repo.path, "rev-list", "--count", t["head"], "--not", "--branches", "--tags", "--remotes")
                orphans = int(out.strip()) if out and out.strip().isdigit() else None
            if orphans != 0:
                base.update(status="keep", reason="Folder is gone, but its detached HEAD is the only reference to "
                                                  "commits on no remote; pruning would orphan them")
            else:
                base.update(status="safe", reason="Folder already gone; only stale metadata remains", action="prune")
        elif admin and any(os.path.exists(os.path.join(admin, m)) for m in BUSY_MARKERS):
            base.update(status="keep", reason="A git operation is in progress in this worktree")
        elif admin and os.path.isdir(os.path.join(admin, "modules")):
            base.update(status="keep", reason="Has initialized submodules; git cannot remove it without --force")
        else:
            base["bytes"] = dir_size(t["path"]) if measure else None
            d = dirty_state(t["path"])
            if not t["head"]:
                unp = None
            elif t["branch"]:
                unp = repo.unpushed_tip(t["branch"], t["head"])
            else:  # detached HEAD: not covered by the branch-wide list
                unp = repo.unpushed(t["head"])
            precious = precious_ignored(t["path"])
            active = recent_activity(t["path"], admin)
            base["precious"] = precious or []
            if d.get("error"):
                base.update(status="keep", reason="Could not read worktree status")
            elif active is not None:
                base.update(status="keep", reason=f"Files changed {max(1, int(active // 60))} min ago; "
                                                  "it may be in use by a coding agent or editor")
            elif d["counts"]["total"]:
                base.update(status="keep", reason=f"{d['counts']['total']} uncommitted change(s)")
            elif unp is None:
                base.update(status="keep", reason="Could not verify against remotes")
            elif unp > 0:
                base.update(status="keep", reason=f"{unp} unpushed commit(s)")
            elif precious is None:
                base.update(status="keep", reason="Could not list ignored files")
            elif repo.features()["shallow"] is not False or repo.features()["partial_clone"] is not False:
                base.update(status="review", reason="Incomplete clone history; verify worktree commits and objects manually")
            elif precious:
                shown = ", ".join(precious[:3]) + ("..." if len(precious) > 3 else "")
                base.update(status="review", reason=f"Clean and pushed, but holds {len(precious)} ignored item(s) "
                                                    f"that exist nowhere else ({shown}); they are backed up before removal")
            elif not fresh:
                base.update(status="review", reason="Clean and pushed at last fetch; fetch first")
            else:
                base.update(status="safe", reason="Clean and fully pushed")
        items.append(base)
    return items


def precious_ignored(path: str) -> list[str] | None:
    """Ignored files in a worktree that are not rebuildable (.env, local config, data).

    `git worktree remove` deletes ignored files without asking, so these keep a worktree out of Safe.
    """
    out = git_ok(path, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory", timeout=90)
    if out is None:
        return None
    precious = []
    for entry in (e for e in out.split("\0") if e):
        name = entry.rstrip("/").rsplit("/", 1)[-1]
        rule = JUNK_RULES.get(name)
        if rule and not rule[2]:  # specific rebuildable folder; generic dist/build/out still count
            continue
        if name.endswith((".pyc", ".pyo")):
            continue
        precious.append(entry)
    return precious


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


def find_junk(repo: Repo, excluded: list[str] | None = None) -> tuple[list[dict], int]:
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
        except OSError as exc:
            context = SIZE_CONTEXT.get()
            if context is not None:
                context["walk_errors"].append(f"{current}: {exc}")
            continue
        for e in entries:
            try:
                if is_excluded(e.path, excluded or []):
                    continue
                if is_link_like(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name == ".git":
                        continue
                    if os.path.exists(os.path.join(e.path, ".git")):
                        continue  # nested repo or linked worktree: scanned separately
                    rel = os.path.relpath(os.path.join(current, e.name), root)
                    if e.name in JUNK_RULES or (e.name in ("venv",) and os.path.exists(os.path.join(e.path, "pyvenv.cfg"))):
                        if contains_excluded(e.path, excluded or []):
                            continue  # never measure or offer an enclosing cleanup that would remove an exclusion
                        candidates.append((rel, e.name))
                        continue
                    if depth < JUNK_DEPTH:
                        stack.append((os.path.join(current, e.name), depth + 1))
                    else:
                        total += dir_size(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                context = SIZE_CONTEXT.get()
                if context is not None:
                    context["walk_errors"].append(f"Could not read entry in {current}")
                continue
    ignored = ignored_set(root, [c[0] for c in candidates])
    items = []
    for rel, name in candidates:
        item = classify_folder(root, rel, ignored)
        if item:
            total += item["bytes"]
            items.append(item)
    return items, total


def classify_folder(root: str, rel: str, ignored: set[str] | None = None, measure: bool = True) -> dict | None:
    """One junk-folder candidate; `ignored` is a precomputed check-ignore result (computed here when None)."""
    name = os.path.basename(rel.rstrip("\\/"))
    rule = JUNK_RULES.get(name)
    full = os.path.join(root, rel)
    if not rule or not os.path.isdir(full) or os.path.islink(full) or is_junction(full):
        return None
    label, manifests, generic = rule
    if name in (".venv", "venv") and not os.path.exists(os.path.join(full, "pyvenv.cfg")):
        return None
    if ignored is None:
        ignored = ignored_set(root, [rel])
    base = {"kind": "folder", "target": rel, "label": label, "bytes": dir_size(full) if measure else None}
    rel_key = rel.replace("\\", "/")
    if not within(full, root):
        base.update(status="keep", reason="Resolves outside the repository")
    elif has_tracked(root, rel):
        base.update(status="keep", reason="Contains files tracked by git")
    elif rel_key not in ignored:
        base.update(status="review", reason="Not git-ignored; confirm it is generated")
    elif generic:
        base.update(status="review", reason="Generic build folder; confirm it can be rebuilt")
    elif not has_manifest(Path(full).parent, manifests):
        base.update(status="review", reason="Rebuild manifest not found next to it")
    else:
        base.update(status="safe", reason="Ignored, untracked and rebuildable")
    return base


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
    item = {"kind": "gc", "target": "git gc", "bytes": loose, "estimate": True, "status": "safe",
            "reason": f"Up to {human(loose)} of loose or garbage objects; git gc packs them (actual savings are usually smaller)"}
    busy = repo.busy()
    if busy:
        item.update(status="keep", reason="A git operation is in progress: " + ", ".join(busy[:3]))
    return item


# ---------------------------------------------------------------- scan

def scan_repo(path: str, do_fetch: bool = False, measure_worktrees: bool = True,
              excluded: list[str] | None = None) -> dict:
    started = now()
    try:
        repo = Repo(path)
    except RuntimeError as exc:
        return {"path": path, "name": os.path.basename(path), "error": str(exc)}
    is_wt = git_kind(repo.path) == "worktree"
    result: dict = {"path": repo.path, "name": repo.name, "remote": repo.remote, "default": repo.default,
                    "kind": "worktree" if is_wt else "repo", "bare": repo.bare}
    if is_wt:
        parent = os.path.normpath(os.path.dirname(repo.git_dir.rstrip("/\\")))
        result.update(parent=parent, parent_name=os.path.basename(parent))
    errors: list[str] = list(repo.read_errors)

    def counts(out: str | None, what: str) -> tuple[int, int]:
        try:
            a, b = (int(x) for x in (out or "").split())
            return a, b
        except ValueError:
            errors.append(f"Could not compare {what}")
            return 0, 0

    if do_fetch and not is_wt:
        result["fetch_error"] = repo.fetch()
    result["fetched_age_h"] = repo.fetched_age_h()
    result["fresh"] = repo.fresh()
    cur = git_ok(repo.path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not repo.has_head and not cur:
        errors.append("Could not resolve HEAD as a commit or an unborn branch")
    result["branch"] = cur.strip() if cur else None
    result["detached"] = cur is None and repo.has_head
    result["features"] = repo.features()
    result["repository_id"] = "repo-" + hashlib.sha256(_norm_key(repo.git_dir).encode("utf-8")).hexdigest()[:24]
    result["id"] = "checkout-" + hashlib.sha256(_norm_key(repo.path).encode("utf-8")).hexdigest()[:24]
    result["head"] = (git_ok(repo.path, "rev-parse", "--verify", "HEAD") or "").strip() or None
    result["unborn"] = not repo.has_head
    result["operations_in_progress"] = repo.busy()
    if os.path.exists(os.path.join(repo.git_dir, "repopulse.lock")):
        result["operations_in_progress"].append("repopulse.lock; inspect recover before continuing")
    result["remotes"] = remote_inventory(repo, errors)
    result["remote_health"] = [] if is_wt else remote_health(repo)
    result["detached_orphans"] = 0
    if result["detached"]:
        out = git_ok(repo.path, "rev-list", "--count", "HEAD", "--not", "--branches", "--remotes", "--tags")
        result["detached_orphans"] = int(out.strip()) if out and out.strip().isdigit() else None
        if result["detached_orphans"] is None:
            errors.append("Could not verify detached HEAD reachability")

    # current branch vs its upstream
    up = git_ok(repo.path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") if cur else None
    result["upstream"] = up.strip() if up else None
    b_ahead = b_behind = 0
    if result["upstream"]:
        b_ahead, b_behind = counts(git_ok(repo.path, "rev-list", "--left-right", "--count", "HEAD...@{u}"),
                                   f"{result['branch']} with {result['upstream']}")
    elif repo.has_head and repo.remote:
        n = repo.unpushed("HEAD")
        if n is None:
            errors.append("Could not count unpushed commits")
        b_ahead = n or 0
    result.update(branch_ahead=b_ahead, branch_behind=b_behind)

    # main vs origin/main
    d, r = repo.default, repo.remote
    state, ahead, behind = "no_main", 0, 0
    if is_wt:
        state = "worktree"
    elif d and repo.ref_exists(f"refs/heads/{d}"):
        if not r or not repo.ref_exists(f"refs/remotes/{r}/{d}"):
            state = "no_remote"
        else:
            out = git_ok(repo.path, "rev-list", "--left-right", "--count", f"refs/heads/{d}...refs/remotes/{r}/{d}")
            ahead, behind = counts(out, f"{d} with {r}/{d}")
            state = "unknown" if out is None else "in_sync" if not ahead and not behind else \
                "diverged" if ahead and behind else "ahead" if ahead else "behind"
    result.update(main_state=state, ahead=ahead, behind=behind)
    last = git_ok(repo.path, "log", "-1", "--format=%ct") if repo.has_head else None
    result["last_commit"] = int(last) if last and last.strip() else None
    if r and d and repo.ref_exists(f"refs/remotes/{r}/{d}"):
        rt = git_ok(repo.path, "log", "-1", "--format=%ct", f"refs/remotes/{r}/{d}")
        result["remote_tip"] = int(rt) if rt and rt.strip() else None

    # dirty + diff preview
    dstate = dirty_state(repo.path) if not repo.bare else parse_status("")
    stats = numstat(repo) if dstate["files"] else {}
    files = []
    for f in dstate["files"][:DIFF_FILES]:
        add, rem = (stats or {}).get(f["path"], (None, None))
        files.append({**f, "added": add, "removed": rem})
    result["dirty"] = {**dstate["counts"], "files": files, "truncated": len(dstate["files"]) > DIFF_FILES}
    if dstate.get("error"):
        result["dirty"]["error"] = dstate["error"]
        errors.append("Could not read working-tree status: " + dstate["error"].splitlines()[-1])
    if stats is None:
        errors.append("Could not read line counts")
    if repo.bare:
        stash = git_ok(repo.path, "reflog", "show", "--format=%H", "refs/stash") if repo.ref_exists("refs/stash") else ""
    else:
        stash = git_ok(repo.path, "stash", "list")
    if stash is None:
        errors.append("Could not list stashes")
    result["stashes"] = len([l for l in (stash or "").splitlines() if l.strip()])
    result["stash_scope"] = "repository"

    # branches, worktrees and git garbage belong to the main repository row only
    trees: list[dict] = []
    items: list[dict] = []
    if not is_wt:
        listed = repo.worktrees()
        if listed is None:
            errors.append("Could not list worktrees")
        trees = listed or []
        branches = classify_branches(repo, trees)
        if branches is None:
            errors.append("Could not list branches")
        visible = [t for t in trees if not is_excluded(t["path"], excluded or [])]
        # Keep all registrations for branch protection, but never inspect excluded checkouts.
        items = (branches or []) + (classify_worktrees(repo, visible, measure_worktrees, excluded) if listed is not None else [])
    else:
        listed = repo.worktrees()
        if listed is None:
            errors.append("Could not list worktrees")
        else:
            trees = listed
            if trees and not trees[0]["bare"]:
                result.update(parent=trees[0]["path"], parent_name=os.path.basename(trees[0]["path"]))
            own = next((t for t in trees if _norm_key(t["path"]) == _norm_key(repo.path)), None)
            if own and own["locked"]:
                result["operations_in_progress"].append("Worktree is locked: " + (own["lock_reason"] or "no reason recorded"))
    junk, wt_bytes = find_junk(repo, excluded) if not repo.bare else ([], 0)
    items += junk
    g = None if is_wt else gc_item(repo)
    if g:
        items.append(g)
    for it in items:
        it["id"] = item_id(it["kind"], repo.path, it["target"])
    result["items"] = items
    result["branches"] = [i for i in items if i["kind"] == "branch"]
    for branch in result["branches"]:
        branch["checked_out_paths"] = [p for p in branch["checked_out_paths"] if not is_excluded(p, excluded or [])]
    if any(b["unpushed"] is None and repo.remote for b in result["branches"]):
        errors.append("Could not verify branch reachability against remotes")
    visible = [t for t in trees if not is_excluded(t["path"], excluded or [])]
    result["worktree_inventory"] = worktree_inventory(repo, visible, items)
    result["unpushed_branches"] = [{"name": b["target"], "commits": b["unpushed"]} for b in items
                                   if b["kind"] == "branch" and b.get("unpushed")]
    result["worktrees"] = 0 if is_wt else len([t for t in visible if not t["main"]])
    result["worktree_bytes"] = wt_bytes
    result["git_bytes"] = 0 if is_wt else dir_size(repo.git_dir)
    errors.extend(e for e in repo.read_errors if e not in errors)
    result["errors"] = errors
    result["health"] = "unknown" if errors else "ok"
    tally_bytes(result)
    result["attention"] = attention(result)
    result["scan_ms"] = int((now() - started) * 1000)
    return result


def tally_bytes(result: dict) -> None:
    items = result["items"]
    result["safe_bytes"] = sum(i["bytes"] or 0 for i in items if i["status"] == "safe")
    result["review_bytes"] = sum(i["bytes"] or 0 for i in items if i["status"] == "review")


def attention(r: dict) -> list[str]:
    notes = list(r.get("errors") or [])
    if r.get("fetch_error"):
        notes.append(f"Fetch failed: {r['fetch_error']}")
    if r.get("detached_orphans"):
        notes.append(f"Detached HEAD has {r['detached_orphans']} commit(s) on no branch; create a branch to keep them")
    notes += [n["text"] for n in r.get("remote_health") or [] if n["level"] == "warn"]
    b = r.get("branch")
    if r["main_state"] == "worktree" or (b and b != r.get("default")):
        if b and r.get("upstream") and r["branch_ahead"] and r["branch_behind"]:
            notes.append(f"{b} diverged from {r['upstream']}: {r['branch_ahead']} ahead, {r['branch_behind']} behind")
        elif b and r.get("upstream") and r["branch_behind"]:
            notes.append(f"{b} is {r['branch_behind']} behind {r['upstream']}")
        elif b and r["branch_ahead"]:
            notes.append(f"{b} has {r['branch_ahead']} unpushed commit(s)" + ("" if r.get("upstream") else " (never pushed)"))
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
    others = [x for x in r["unpushed_branches"] if x["name"] not in (r.get("default"), b)]
    if others:
        notes.append(f"{len(others)} other branch(es) with unpushed work")
    if r["stashes"]:
        notes.append(f"{r['stashes']} stash(es)")
    if r.get("remote") and not r.get("fresh") and r["main_state"] != "worktree":
        notes.append("Not fetched in 24 h")
    return notes


def scan_caches() -> list[dict]:
    items, seen = [], set()
    for label, paths, status in CACHE_DIRS:
        for p in paths:  # every location that exists, not only the first (WSL-style homes, mixed installs)
            path = expand(p)
            if not path or not path.is_dir() or path.is_symlink() or is_junction(str(path)):
                continue
            key = os.path.normcase(os.path.realpath(path))
            if key in seen:
                continue
            seen.add(key)
            size = dir_size(path)
            reason = "Re-downloaded on demand; offline installs need network afterwards" if status == "safe" \
                else "Shared by every project on this machine; rebuild needs network or a prune command"
            items.append({"kind": "cache", "target": str(path), "label": label, "bytes": size, "status": status,
                          "reason": reason, "id": item_id("cache", "", str(path))})
    return items


def read_previous_sizes(path: str | None) -> dict:
    """External snapshots are hints about size only, never evidence of safety."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        previous = json.load(fh)
    if not isinstance(previous, dict) or previous.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Previous snapshot has an unsupported schema_version")
    sizes = {}
    for row in previous.get("repos", []):
        for key, value in row.get("disk", {}).get("measurements", {}).items():
            if (isinstance(value, dict) and value.get("complete") is True and
                    type(value.get("bytes")) is int and value["bytes"] >= 0 and
                    type(value.get("measured_at")) in (int, float) and 0 < value["measured_at"] <= now()):
                sizes[_norm_key(key)] = value
    return sizes


def scan_with_sizes(path: str, previous: dict, max_age: float, excluded: list[str] | None = None) -> dict:
    context = {"previous": previous, "max_age": max_age, "measurements": {}, "walk_errors": []}
    token = SIZE_CONTEXT.set(context)
    try:
        row = scan_repo(path, False, False, excluded)
        row["disk"] = {"measurements": context["measurements"],
                       "reused_measurements": sum(m["reused"] for m in context["measurements"].values()),
                       "estimated": any(m["reused"] for m in context["measurements"].values()),
                       "complete": not context["walk_errors"] and all(m["complete"] for m in context["measurements"].values()),
                       "errors": context["walk_errors"]}
        return row
    finally:
        SIZE_CONTEXT.reset(token)


def cmd_scan(args) -> dict:
    started = now()
    diagnostics: list[dict] = []
    previous = read_previous_sizes(getattr(args, "previous", None))
    roots = (getattr(args, "root", None) or []) + [r for r in (args.roots or "").split(";") if r.strip()]
    roots = roots or default_roots()
    exclusions = (getattr(args, "exclude_path", None) or []) + [p for p in (args.exclude or "").split(";") if p.strip()]
    excluded = [_norm_key(p) for p in exclusions]
    if args.repo:
        entries = [{"path": os.path.abspath(os.path.expanduser(p)), "kind": git_kind(os.path.expanduser(p)) or "repo", "source": "argument"}
                   for p in args.repo if not is_excluded(p, excluded)]
        if args.cmd == "snapshot":
            linked = discover([e["path"] for e in entries], 0, hints=False, exclude=exclusions, diagnostics=diagnostics)
            entries += linked
        entries = list({_norm_key(e["path"]): e for e in entries if not is_excluded(e["path"], excluded)}.values())
        roots = sorted({_norm_key(p) for p in args.repo})
    else:
        entries = discover(roots, args.depth, hints=not args.no_hints, exclude=exclusions, diagnostics=diagnostics)
    workers = max(1, min(getattr(args, "workers", 8), len(entries)))
    fetch_errors: dict[str, str | None] = {}
    if args.fetch:  # fetch every main repo first, so worktrees read fresh refs
        mains = [e["path"] for e in entries if e["kind"] == "repo"]
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for path, err in zip(mains, pool.map(_fetch_one, mains)):
                fetch_errors[os.path.normcase(path)] = err
    results = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_with_sizes, e["path"], previous, getattr(args, "max_size_age", 300), excluded): e for e in entries}
        for fut in cf.as_completed(futures):
            e = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:  # one broken repo must not fail the scan
                r = {"path": e["path"], "name": os.path.basename(e["path"]), "error": str(exc)}
            r["source"] = e["source"]
            r.setdefault("kind", e["kind"])
            err = fetch_errors.get(os.path.normcase(e["path"]))
            if err and not r.get("error"):
                r["fetch_error"] = err
                r["attention"] = attention(r)
            results.append(r)
    # worktree items were left unmeasured: reuse each worktree row's walk instead of walking the tree twice
    row_bytes = {os.path.normcase(os.path.normpath(r["path"])): r["worktree_bytes"] for r in results
                 if r.get("kind") == "worktree" and not r.get("error")}
    for r in results:
        if r.get("error"):
            continue
        for it in r["items"]:
            if it["kind"] == "worktree" and it["bytes"] is None:
                key = os.path.normcase(os.path.normpath(it["target"]))
                it["bytes"] = row_bytes[key] if key in row_bytes else dir_size(it["target"])
        sizes = {_norm_key(i["target"]): i["bytes"] for i in r["items"] if i["kind"] == "worktree"}
        for tree in r.get("worktree_inventory", []):
            if _norm_key(tree["path"]) in sizes:
                tree["bytes"] = sizes[_norm_key(tree["path"])]
        tally_bytes(r)
    recovery = cmd_recover(argparse.Namespace(dest=getattr(args, "dest", None), fix=False))
    for r in results:
        r.setdefault("id", "checkout-" + hashlib.sha256(_norm_key(r["path"]).encode("utf-8")).hexdigest()[:24])
        r.setdefault("repository_id", None)
        r["observed_at"] = int(now())
        r["agent_origin"] = agent_origin(r["path"])
        r["interrupted_operations"] = [op for op in recovery["interrupted"] if op.get("repo") and _norm_key(op["repo"]) == _norm_key(r["path"])]
        if r["interrupted_operations"]:
            r.setdefault("operations_in_progress", []).append("Interrupted Repo Pulse cleanup; inspect recovery advice")
        if not r.get("disk", {}).get("complete", True):
            r.setdefault("errors", []).append("Disk inventory is incomplete; some directories could not be read")
            r["health"] = "unknown"
        assess_readiness(r)
        # Readiness must account for locks and dirty state in the default branch's holder.
        if r["readiness"]["sync"]["state"] == "safe":
            holders = [t for t in r.get("worktree_inventory", []) if t.get("branch") == r.get("default")]
            for holder in holders:
                target = next((x for x in results if _norm_key(x["path"]) == _norm_key(holder["path"])), None)
                if holder["locked"] or not holder["exists"] or target is None or target.get("error") or target.get("dirty", {}).get("total") or target.get("health") == "unknown":
                    r["readiness"]["sync"] = {"state": "review", "reason": "Default branch holder is locked, unavailable, dirty or unverified"}
        if not r.get("error"):
            r["attention"] = attention(r)
            if r.get("operations_in_progress"):
                r["attention"].append("Git or Repo Pulse operation needs inspection")
            for i in r["items"]:
                i["recovery"] = recovery_hint(i, False)
    results.sort(key=lambda r: (not r.get("attention"), (r.get("parent_name") or r["name"]).lower(),
                                r.get("kind") == "worktree", r["name"].lower(), _norm_key(r["path"])))
    caches = scan_caches() if args.caches else []
    ok = [r for r in results if not r.get("error")]
    # a worktree row's folders are already inside a Safe worktree item of its main repo: count them once
    reclaimed = reclaimable_totals(ok, caches)
    sources: dict[str, int] = {}
    for r in results:
        sources[r["source"]] = sources.get(r["source"], 0) + 1
    complete = not diagnostics and all(not r.get("error") and r.get("health") == "ok" for r in results)
    return {
        "schema_version": SCHEMA_VERSION, "producer": "repo-pulse", "complete": complete,
        "read_only": not args.fetch, "diagnostics": diagnostics, "recovery": recovery,
        "refresh": {"git": "live", "size_max_age_seconds": getattr(args, "max_size_age", 300),
                    "reused_measurements": sum(r.get("disk", {}).get("reused_measurements", 0) for r in results)},
        "ok": True, "version": VERSION, "scanned_at": int(now()), "roots": roots, "repos": results, "caches": caches,
        "sources": sources, "scan_ms": int((now() - started) * 1000),
        "totals": {
            "repos": len([r for r in results if r.get("kind") != "worktree"]),
            "worktrees": len([r for r in results if r.get("kind") == "worktree"]),
            "needs_attention": len([r for r in results if r.get("error") or r.get("attention")]),
            "safe_bytes": reclaimed["safe"], "review_bytes": reclaimed["review"],
            "worktree_bytes": sum(r["worktree_bytes"] for r in ok),
            "git_bytes": sum(r["git_bytes"] for r in ok),
        },
    }


def reclaimable_totals(rows: list[dict], caches: list[dict]) -> dict:
    """Count overlapping worktree/folder/cache estimates only once, across both risk bands."""
    candidates = []
    totals = {"safe": 0, "review": 0}
    for row in rows:
        for item in row["items"]:
            if item["status"] not in totals:
                continue
            if item["kind"] in ("folder", "worktree"):
                path = os.path.join(row["path"], item["target"]) if item["kind"] == "folder" else item["target"]
                candidates.append((_norm_key(path), item))
            else:
                totals[item["status"]] += item["bytes"] or 0
    candidates.extend((_norm_key(c["target"]), c) for c in caches if c["status"] in totals)
    included = []
    for path, item in sorted(candidates, key=lambda pair: (len(pair[0]), pair[0])):
        if any(path == p or path.startswith(p.rstrip("\\/") + os.sep) for p in included):
            continue
        included.append(path)
        totals[item["status"]] += item["bytes"] or 0
    return totals


def snapshot(*, roots: list[str] | None = None, repos: list[str] | None = None,
             exclude: list[str] | None = None, hints: bool = False, workers: int = 4,
             previous: str | None = None, max_size_age: int = 300, backup_dir: str | None = None) -> dict:
    """Read-only Python API for Aura. No fetch, action, callback, shell or write option."""
    argv = ["snapshot", "--workers", str(workers), "--max-size-age", str(max_size_age)]
    for option, paths in (("--root", roots), ("--repo", repos), ("--exclude-path", exclude)):
        for path in paths or []:
            argv.extend((option, path))
    if not hints:
        argv.append("--no-hints")
    if previous:
        argv.extend(("--previous", previous))
    if backup_dir:
        argv.extend(("--dest", backup_dir))
    return cmd_scan(build_parser().parse_args(argv))


def _fetch_one(path: str) -> str | None:
    try:
        return Repo(path).fetch()
    except RuntimeError as exc:
        return str(exc)


# ---------------------------------------------------------------- actions

def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]


def patch_applies(repo: Repo, patch: Path, base: str) -> bool | None:
    """Restore drill: does the patch apply cleanly to `base`? Uses a throwaway index, so the repository's
    own index and working tree are never touched. None when git could not run the check."""
    fd, idx = tempfile.mkstemp(prefix="repopulse-index-")
    os.close(fd)
    os.remove(idx)  # read-tree wants to create it
    env = {"GIT_INDEX_FILE": idx}
    try:
        code, _, _ = git(repo.path, "read-tree", base, extra_env=env)
        if code != 0:
            return None
        code, _, _ = git(repo.path, "apply", "--cached", "--check", "--binary", str(patch), extra_env=env, timeout=120)
        return code == 0
    finally:
        for p in (idx, idx + ".lock"):
            try:
                os.remove(p)
            except OSError:
                pass


def bundle_holds(repo: Repo, bundle: Path, shas: list[str]) -> str | None:
    """None when the bundle verifies against this repository and carries every sha as a ref tip; else why not."""
    code, _, err = git(repo.path, "bundle", "verify", "--quiet", str(bundle), timeout=600)
    if code != 0:
        return f"bundle does not verify: {err}"
    code, out, err = git(repo.path, "bundle", "list-heads", str(bundle), timeout=600)
    if code != 0:
        return f"could not list bundle refs: {err}"
    tips = {line.split()[0] for line in out.splitlines() if line.strip()}
    missing = [s for s in shas if s and s not in tips]
    return f"bundle is missing {len(missing)} expected ref(s)" if missing else None


def repo_backup_dir(dest: str | None, repo: Repo) -> Path:
    return backup_root(dest) / f"{safe_name(repo.name)}-{path_tag(repo.path)}"


def cmd_backup(args) -> dict:
    repo = Repo(args.repo)
    branch = (git_ok(repo.path, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip() or None
    head = (git_ok(repo.path, "rev-parse", "--verify", "-q", "HEAD") or "").strip() or None
    detached = head is not None and branch is None
    problems: list[dict] = []  # anything not saved and proven restorable makes the backup incomplete
    changed: set[str] = set()
    deleted: list[str] = []
    listing = [("diff", "--name-only", "-z", "HEAD")] if repo.has_head else [("ls-files", "-z", "--cached")]
    for cmd in listing + [("ls-files", "-z", "--others", "--exclude-standard")]:
        out = git_ok(repo.path, *cmd)
        if out is None:
            problems.append({"part": "files", "error": f"git {cmd[0]} failed; changed files may be missing"})
        changed.update(p for p in (out or "").split("\0") if p)
    total = 0
    present, links = [], []
    for rel in sorted(changed):
        src = os.path.join(repo.path, rel)
        try:
            st = os.lstat(longp(src))
        except FileNotFoundError:
            deleted.append(rel)
            continue
        except OSError as exc:
            problems.append({"part": "file", "path": rel, "error": exc.strerror or str(exc)})
            continue
        if stat.S_ISLNK(st.st_mode):
            try:
                links.append({"path": rel, "target": os.readlink(src)})  # recorded, never followed
            except OSError as exc:
                problems.append({"part": "file", "path": rel, "error": exc.strerror or str(exc)})
        elif not within(os.path.dirname(src) or repo.path, repo.path) and os.path.dirname(rel):
            problems.append({"part": "file", "path": rel, "error": "a parent folder links outside the repository; not followed"})
        elif stat.S_ISREG(st.st_mode):
            total += st.st_size
            present.append(rel)
    limit = int(args.max_gb * 1024 ** 3)
    if total > limit:
        return {"ok": False, "error": f"Dirty files total {human(total)}, above the {args.max_gb:g} GB backup limit. "
                                      f"Raise the limit or commit/ignore large assets first.", "bytes": total}
    parent = repo_backup_dir(args.dest, repo)
    need = total * 2 + 64 * 1024 * 1024  # files plus patches of them, and room for bundles
    free = free_bytes(parent)
    if free is not None and free < need:
        return {"ok": False, "error": f"Not enough free space for the backup at {parent}: {human(free)} free, "
                                      f"about {human(need)} needed.", "bytes": total}
    try:
        dest = unique_dir(parent, stamp())
        marker = dest / "INCOMPLETE"
        marker.write_text("This backup was interrupted or failed verification. Do not rely on it.\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Cannot write to the backup folder {parent}: {exc.strerror or exc}"}

    encrypt = bool(getattr(args, "encrypt", False))
    hashes: dict[str, str] = {}
    for rel in present:  # a locked, vanished or changing file must not abort the rest of the backup
        try:
            hashes["files/" + rel] = copy_verified(os.path.join(repo.path, rel), dest / "files" / rel, encrypt)
        except OSError as exc:
            problems.append({"part": "file", "path": rel, "error": exc.strerror or str(exc)})

    def save_patch(name: str, raw: bytes, base: str) -> bool:
        path = dest / name
        path.write_bytes(raw)
        hashes[name] = file_sha256(path)
        ok = patch_applies(repo, path, base)
        if ok is not True:
            problems.append({"part": "patch", "path": name, "error": "could not test that it restores" if ok is None
                             else f"does not apply cleanly to {base}"})
        if encrypt:
            try:
                (dest / (name + ENC_SUFFIX)).write_bytes(encrypt_bytes(raw))
                path.unlink()
            except OSError as exc:
                problems.append({"part": "patch", "path": name, "error": exc.strerror or str(exc)})
        return ok is True

    if repo.has_head:
        for name, extra in (("changes.patch", ["HEAD"]), ("staged.patch", ["--cached"])):
            code, raw = git_bytes(repo.path, "diff", "--binary", *extra, timeout=120)
            if code != 0:
                problems.append({"part": "patch", "path": name, "error": "git diff failed"})
            elif raw:
                save_patch(name, raw, "HEAD")
    stash_list = git_ok(repo.path, "stash", "list")
    if stash_list is None:
        problems.append({"part": "stash", "error": "could not list stashes"})
    stash_n = len([l for l in (stash_list or "").splitlines() if l.strip()])
    # the bundle can only carry refs/stash (the newest entry); every entry is also saved as a patch,
    # with its base commit and message so `restore` can rebuild it as a real stash entry
    stash_patches = []
    stash_meta = []
    for line in (git_ok(repo.path, "stash", "list", "--format=%H%x00%P%x00%gs") or "").splitlines():
        sha, parents, msg = (line.split("\0") + ["", "", ""])[:3]
        stash_meta.append({"sha": sha, "base": parents.split()[0] if parents else None, "message": msg})
    for n in range(stash_n):
        code, raw = git_bytes(repo.path, "stash", "show", "-p", "--binary", "--include-untracked", f"stash@{{{n}}}", timeout=120)
        if code != 0:  # git < 2.32 has no --include-untracked for stash show
            code, raw = git_bytes(repo.path, "stash", "show", "-p", "--binary", f"stash@{{{n}}}", timeout=120)
        if code != 0:
            problems.append({"part": "stash", "path": f"stash@{{{n}}}", "error": "git stash show failed"})
        elif raw:
            save_patch(f"stash-{n}.patch", raw, f"stash@{{{n}}}^1")
            stash_patches.append(f"stash-{n}.patch")

    bundle = None
    refs = ["--branches"]
    if detached:
        refs.append("HEAD")
    stash_sha = (git_ok(repo.path, "rev-parse", "--verify", "-q", "refs/stash") or "").strip()
    if stash_sha:
        refs.append("refs/stash")
    if repo.remote and detached:
        out = git_ok(repo.path, "rev-list", "--count", "--branches", "HEAD", "--not", "--remotes")
        unp = int(out.strip()) if out and out.strip().isdigit() else None
    else:
        unp = repo.unpushed("--branches") if repo.remote else None
    detached_unp = repo.unpushed("HEAD") if detached and repo.remote else None
    if detached and repo.remote and detached_unp is None:
        problems.append({"part": "bundle", "error": "could not verify detached HEAD reachability"})
    if repo.remote and unp is None:
        problems.append({"part": "bundle", "error": "could not count unpushed commits"})
    lfs = repo_features(repo).get("lfs")
    if lfs and unp:
        problems.append({"part": "bundle", "error": "Git LFS: the bundle carries commits but not LFS file contents; "
                                                    "run git lfs push or keep the repository until they are pushed"})
    if repo.has_head and (unp or stash_n or not repo.remote):
        neg = ["--not", "--remotes"] if repo.remote else []
        path = dest / "unpushed.bundle"
        code, _, err = git(repo.path, "bundle", "create", str(path), *refs, *neg, timeout=600)
        expected = [stash_sha] + ([head] if detached and (detached_unp or not repo.remote) else [])
        why = (f"bundle failed: {err}" if code != 0 else bundle_holds(repo, path, expected))
        if why:
            problems.append({"part": "bundle", "path": "unpushed.bundle", "error": why})
        else:
            hashes["unpushed.bundle"] = file_sha256(path)
        bundle = {"file": "unpushed.bundle", "ok": why is None, "error": why, "unpushed_commits": unp, "stashes": stash_n}

    complete = not problems
    manifest = {
        "format": 2, "repo": repo.path, "created": dt.datetime.now().isoformat(timespec="seconds"), "head": head,
        "branch": branch,
        "complete": complete, "files": sorted(k[6:] for k in hashes if k.startswith("files/")),
        "sha256": hashes,  # keyed by path inside this backup folder
        "links": links, "failed": [p for p in problems if p["part"] == "file"], "problems": problems,
        "deleted": deleted, "bytes": total, "bundle": bundle, "stash_patches": stash_patches, "stashes": stash_meta,
        "encrypted": "dpapi-user" if encrypt else None,
        "restore": [
            "Everything: python repopulse.py restore --backup <this folder> (never overwrites without --overwrite).",
            "Files: copy files/ over the repository (or apply changes.patch with: git apply --binary changes.patch).",
            "Commits: git fetch <path>/unpushed.bundle '+refs/heads/*:refs/repopulse-backup/*' then inspect "
            "refs/repopulse-backup/<branch> (not under refs/remotes, so it never makes local work look pushed).",
            "Stashes: git apply --binary stash-N.patch (stash-0 is the newest).",
            "Check: python repopulse.py verify --backup <path> re-hashes every file against this manifest.",
        ],
    }
    write_json_atomic(dest / "manifest.json", manifest)
    if complete:
        marker.unlink()
    log_operation(args.dest, {"op": "backup", "repo": repo.path, "backup": str(dest), "complete": complete,
                              "problems": len(problems), "head": head})
    summary = "; ".join(f"{p.get('path') or p['part']}: {p['error']}" for p in problems[:3])
    return {"ok": complete, "complete": complete, "backup": str(dest), "files": len(manifest["files"]),
            "failed": manifest["failed"], "problems": problems, "deleted": len(deleted), "links": len(links),
            "bytes": total, "bundle": bundle, "stash_patches": len(stash_patches),
            "error": None if complete else f"Backup is INCOMPLETE ({len(problems)} problem(s)): {summary}"}


def cmd_verify(args) -> dict:
    """Re-check a backup made by `backup`: every hashed file present and unchanged, bundle still valid."""
    dest = Path(args.backup)
    try:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"No readable manifest.json in {dest}: {exc}"}
    bad = []
    for rel, digest in (manifest.get("sha256") or {}).items():
        path = dest / rel
        if not within(str(path), str(dest)):
            bad.append({"path": rel, "error": "manifest entry points outside the backup"})
            continue
        try:
            if hashlib.sha256(read_backup_file(path)).hexdigest() != digest:
                bad.append({"path": rel, "error": "content changed"})
        except OSError as exc:
            bad.append({"path": rel, "error": exc.strerror or str(exc)})
    if (dest / "unpushed.bundle").exists() and manifest.get("repo") and os.path.isdir(manifest["repo"]):
        why = bundle_holds(Repo(manifest["repo"]), dest / "unpushed.bundle", [])
        if why:
            bad.append({"path": "unpushed.bundle", "error": why})
    complete = bool(manifest.get("complete", True)) and not (dest / "INCOMPLETE").exists()
    return {"ok": not bad and complete, "complete": complete, "checked": len(manifest.get("sha256") or {}), "bad": bad,
            "error": None if not bad and complete else
            ("Backup was recorded as incomplete" if not bad else f"{len(bad)} file(s) failed verification")}


def cmd_sync(args) -> dict:
    repo = Repo(args.repo)
    if not repo.remote or not repo.default:
        return {"ok": False, "error": "No remote or default branch to sync with."}
    if repo.busy() or os.path.exists(os.path.join(repo.git_dir, "repopulse.lock")):
        return {"ok": False, "error": "Git or Repo Pulse operation is in progress; inspect it before syncing."}
    err = repo.fetch()
    if err:
        return {"ok": False, "error": f"Fetch failed: {err}"}
    d, r = repo.default, repo.remote
    if not repo.ref_exists(f"refs/remotes/{r}/{d}") or not repo.ref_exists(f"refs/heads/{d}"):
        return {"ok": False, "error": f"{d} or {r}/{d} does not exist."}
    out = git_ok(repo.path, "rev-list", "--left-right", "--count", f"refs/heads/{d}...refs/remotes/{r}/{d}")
    if out is None:
        return {"ok": False, "error": "Could not compare default branch with remote; sync refused."}
    ahead, behind = (int(x) for x in out.split())
    steps = []
    if ahead and behind:
        return {"ok": False, "state": "diverged", "ahead": ahead, "behind": behind,
                "error": f"{d} has diverged ({ahead} ahead, {behind} behind). Repo Pulse does not merge or rebase."}
    if behind:
        trees = repo.worktrees()
        if trees is None:
            return {"ok": False, "error": "Could not list worktrees; sync refused."}
        holder = next((t for t in trees if t.get("branch") == d and not t["bare"]), None)
        if holder:
            if holder["locked"] or not os.path.isdir(holder["path"]):
                return {"ok": False, "error": "Default branch worktree is locked or unavailable; sync refused."}
            st = dirty_state(holder["path"])
            if st.get("error"):
                return {"ok": False, "error": "Could not read default branch worktree status; sync refused."}
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


def recycle(path: str) -> str | None:
    """Move to the Recycle Bin (Windows) or to `RepoPulse Backups/Trash` (elsewhere; returns where)."""
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
        return None
    trash = unique_dir(backup_root(os.environ.get("REPOPULSE_TRASH")) / "Trash", f"{stamp()}-{path_tag(path)}")
    target = trash / safe_name(os.path.basename(path))
    shutil.move(path, str(target))
    return str(target)


def unrecycle(path: str, trash: str | None) -> None:
    """Put a recycled folder back where it was. Never overwrites: refuses when the path exists again."""
    if os.path.lexists(path):
        raise RuntimeError(f"{path} exists again; nothing was restored")
    if trash:
        if not os.path.exists(trash):
            raise RuntimeError("It is no longer in the Repo Pulse trash folder")
        shutil.move(trash, path)
        return
    if not IS_WIN:
        raise RuntimeError("No trash location was recorded")
    # Shell.Application lists the Recycle Bin with each item's original folder; move the newest match back.
    script = (
        "$ErrorActionPreference='Stop'; $sh = New-Object -ComObject Shell.Application; $bin = $sh.Namespace(10); "
        "$t = $env:RP_TARGET; $parent = [System.IO.Path]::GetDirectoryName($t); $leaf = [System.IO.Path]::GetFileName($t); "
        "$hit = @($bin.Items() | Where-Object { $_.Name -eq $leaf -and $bin.GetDetailsOf($_, 1) -eq $parent }) | "
        "Sort-Object { $_.ModifyDate } -Descending | Select-Object -First 1; "
        "if (-not $hit) { exit 3 }; Move-Item -LiteralPath $hit.Path -Destination $t"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           timeout=120, env=dict(os.environ, RP_TARGET=os.path.abspath(path)), creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not reach the Recycle Bin: {exc}")
    if r.returncode == 3:
        raise RuntimeError("Not found in the Recycle Bin (emptied, or restored already)")
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("The Recycle Bin did not restore it: " + r.stderr.decode("utf-8", "replace").strip()[-300:])


def remove_permanently(path: str) -> None:
    def retry_writable(func, p, _exc):  # read-only files (common in .git packs and node_modules) on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(longp(path), onexc=retry_writable)
    else:
        shutil.rmtree(longp(path), onerror=retry_writable)


def within(child: str, parent: str) -> bool:
    c, p = os.path.normcase(os.path.realpath(child)), os.path.normcase(os.path.realpath(parent))
    return c != p and os.path.commonpath([c, p]) == p


LOCK_STALE_HOURS = 6


def acquire_lock(repo: Repo) -> Path | None:
    """One cleanup per repository at a time (two widget windows, or the CLI and the widget)."""
    p = Path(repo.git_dir) / "repopulse.lock"
    try:
        if now() - p.stat().st_mtime > LOCK_STALE_HOURS * 3600:
            p.unlink()  # left behind by a crash or forced termination
    except OSError:
        pass
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "at": now()}))
    return p


def revalidate(repo_path: str | None, seen: dict) -> dict | None:
    """The item as it is right now, classified from scratch; None when it no longer exists.

    Runs immediately before each action, so earlier actions in the same batch (a removed worktree frees
    its branch, a deleted branch changes what is pushed) and other tools or agents are always accounted for.
    """
    kind = seen["kind"]
    if kind == "cache":
        return next((c for c in scan_caches() if c["id"] == seen["id"]), None)
    repo = Repo(repo_path)
    fresh = None
    if kind in ("branch", "worktree"):
        trees = repo.worktrees()
        if trees is None:
            raise RuntimeError("Could not list worktrees")
        if kind == "branch":
            got = classify_branches(repo, trees, only=seen["target"])
            if got is None:
                raise RuntimeError("Could not list branches")
        else:
            want = os.path.normcase(os.path.normpath(seen["target"]))
            got = classify_worktrees(repo, [t for t in trees if os.path.normcase(os.path.normpath(t["path"])) == want], measure=False)
        fresh = got[0] if got else None
    elif kind == "folder":
        fresh = classify_folder(repo.path, seen["target"], measure=False)
    elif kind == "gc":
        fresh = gc_item(repo)
    if fresh is None:
        return None
    fresh["id"] = item_id(kind, repo.path, fresh["target"])
    if fresh.get("bytes") is None or kind in ("folder", "worktree"):
        fresh["bytes"] = seen.get("bytes") or 0  # sizes come from the scan; re-measuring would double the cost
    return fresh if fresh["id"] == seen["id"] else None


def cmd_clean(args) -> dict:
    ids = [i for i in (args.ids or "").split(",") if i]
    review_ids = {i for i in (args.review_ids or "").split(",") if i}
    if not ids:
        return {"ok": False, "error": "No item ids given."}
    if args.repo:
        fresh_scan = scan_repo(args.repo)
        if fresh_scan.get("error"):
            return {"ok": False, "error": fresh_scan["error"]}
        current = {i["id"]: i for i in fresh_scan["items"]}
        repo = Repo(args.repo)
        lock = None if args.dry_run else acquire_lock(repo)
        if lock is None and not args.dry_run:
            return {"ok": False, "error": "Another Repo Pulse cleanup is running on this repository; try again when it ends."}
        dest_parent = repo_backup_dir(args.dest, repo)
    else:
        current = {i["id"]: i for i in scan_caches()}
        repo, lock = None, None
        dest_parent = backup_root(args.dest) / "_caches"
    holder: dict[str, Path] = {}

    def get_dest() -> Path:  # created on first use, never shared with another run
        if "d" not in holder:
            holder["d"] = unique_dir(dest_parent, f"cleanup-{stamp()}")
        return holder["d"]

    results = []
    try:
        for iid in ids:
            seen = current.get(iid)
            if not seen:
                results.append({"id": iid, "done": False, "reason": "Changed since the scan; rescan first"})
                continue
            row = {"id": iid, "target": seen["target"], "kind": seen["kind"], "done": False}
            try:
                it = revalidate(args.repo, seen)
            except Exception as exc:
                results.append({**row, "reason": f"Could not re-check, kept: {exc}"})
                continue
            if it is None:
                results.append({**row, "reason": "No longer exists or changed; rescan first"})
                continue
            row["status"] = it["status"]
            # Review needs its own approval: an item selected as Safe that is Review now is never removed
            approved = it["status"] == "safe" or (it["status"] == "review" and (iid in review_ids or args.allow_review))
            if not approved:
                hint = " (it was Safe when you selected it; select it again to approve it as Review)" \
                    if it["status"] == "review" else ""
                results.append({**row, "reason": f"Status is {it['status']}: {it['reason']}{hint}"})
                continue
            if args.dry_run:
                results.append({**row, "would": True, "bytes": it["bytes"], "reason": it["reason"],
                                "recovery": recovery_hint(it, args.permanent)})
                continue
            op_id = new_op_id()
            entry = {"op": "clean", "op_id": op_id, "repo": repo.path if repo else None, "kind": it["kind"],
                     "target": it["target"], "head": it.get("head"), "branch": it.get("branch"), "status": it["status"],
                     "action": it.get("action")}
            log_operation(args.dest, {**entry, "phase": "started"})
            undo = None
            try:
                out = execute(repo, it, get_dest, args.permanent, encrypt=bool(getattr(args, "encrypt", False)))
                undo = out.get("undo")
                results.append({**row, "done": True, "op_id": op_id, "bytes": out.get("bytes", it["bytes"]),
                                "note": out["note"], "recovery": out.get("recovery"),
                                "irreversible": out.get("irreversible", False), "undoable": undo is not None})
            except Exception as exc:
                results.append({**row, "op_id": op_id, "reason": str(exc)})
            last = results[-1]
            log_operation(args.dest, {**entry, "phase": "finished", "done": last["done"], "bytes": last.get("bytes"),
                                      "note": last.get("note") or last.get("reason"), "recovery": last.get("recovery"),
                                      "irreversible": last.get("irreversible", False), "undo": undo})
    finally:
        if lock:
            try:
                lock.unlink()
            except OSError:
                pass
    freed = sum(r.get("bytes") or 0 for r in results if r["done"])
    would = sum(r.get("bytes") or 0 for r in results if r.get("would"))
    out = {"ok": True, "results": results, "freed_bytes": freed, "backup": str(holder["d"]) if "d" in holder else None}
    if args.dry_run:
        out.update(dry_run=True, would_free_bytes=would)
    return out


def recovery_hint(it: dict, permanent: bool) -> str:
    kind = it["kind"]
    if kind == "branch":
        return "Saved as a verified git bundle first; restore with git fetch <bundle> NAME:NAME"
    if kind == "worktree":
        return "Metadata only; nothing to restore" if it.get("action") == "prune" else \
            "Branch and commits are kept; recreate with git worktree add. Ignored files are copied to the backup first"
    if kind in ("folder", "cache"):
        return "Permanent; rebuild or re-download it" if permanent else "Recycle Bin"
    if kind == "gc":
        return "Not reversible, but removes no reachable objects"
    return ""


def execute(repo: Repo | None, it: dict, get_dest, permanent: bool, encrypt: bool = False) -> dict:
    """Carry out one re-validated item. Returns note, recovery text, and `undo` data when it can be undone."""
    kind = it["kind"]
    if kind == "branch":
        name, head = it["target"], it["head"]
        if (git_ok(repo.path, "rev-parse", "--verify", "-q", f"refs/heads/{name}") or "").strip() != head:
            raise RuntimeError("Branch changed since it was checked (another tool or agent moved it); kept")
        tag = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]  # feature/x and feature_x must not share a file
        bundle = unique_file(get_dest(), f"branch-{safe_name(name)}-{tag}", ".bundle")
        code, _, err = git(repo.path, "bundle", "create", str(bundle), f"refs/heads/{name}", timeout=600)
        if code != 0:
            raise RuntimeError(f"Bundle failed, branch kept: {err}")
        why = bundle_holds(repo, bundle, [head])
        if why:
            raise RuntimeError(f"Bundle check failed, branch kept: {why}")
        # compare-and-delete: refused when another tool or agent moved the branch after it was checked
        code, _, err = git(repo.path, "update-ref", "-m", "repo-pulse: delete merged branch", "-d", f"refs/heads/{name}", head)
        if code != 0:
            raise RuntimeError(f"Branch changed since it was checked, kept (bundle saved anyway): {err}")
        git(repo.path, "config", "--remove-section", f"branch.{name}")  # upstream settings; absent is fine
        restore = f"git fetch \"{bundle}\" {name}:{name}"
        return {"note": f"Saved to {bundle.name}; restore with: {restore}", "recovery": restore,
                "undo": {"type": "branch", "repo": repo.path, "name": name, "head": head, "bundle": str(bundle)}}
    if kind == "worktree":
        if it.get("action") == "prune":
            admin = worktree_admin_dir(repo, it["target"])
            if admin is None:
                raise RuntimeError("Worktree metadata not found; it may already be pruned")
            if os.path.exists(os.path.join(it["target"], ".git")):
                raise RuntimeError("The worktree folder is back; kept")
            if os.path.exists(os.path.join(admin, "locked")):
                raise RuntimeError("Worktree is locked; kept")
            remove_permanently(admin)  # this entry only; `git worktree prune` would touch every stale entry
            return {"note": "Pruned stale worktree metadata", "irreversible": True}
        saved = None
        if it.get("precious"):
            sub = unique_dir(get_dest(), f"worktree-{safe_name(os.path.basename(it['target']))}-{path_tag(it['target'])}")
            hashes, links = {}, []
            for rel in it["precious"]:
                h, problems = copy_tree_verified(it["target"], rel, sub / "ignored", encrypt)
                hashes.update(h)
                errors = [p for p in problems if not p.get("link")]
                links += [p["path"] for p in problems if p.get("link")]
                if errors:
                    raise RuntimeError(f"Could not back up ignored file {errors[0]['path']} ({errors[0]['error']}); worktree kept")
            write_json_atomic(sub / "manifest.json", {"format": 2, "kind": "worktree-ignored", "repo": repo.path,
                                                      "worktree": it["target"], "branch": it.get("branch"), "head": it.get("head"),
                                                      "sha256": {"ignored/" + k: v for k, v in hashes.items()},
                                                      "links_not_followed": links, "complete": True,
                                                      "encrypted": "dpapi-user" if encrypt else None,
                                                      "restore": "python repopulse.py restore --backup <this folder> "
                                                                 "copies ignored/ back into the worktree."})
            saved = sub
        code, _, err = git(repo.path, "worktree", "remove", it["target"], timeout=600)  # no --force: git refuses dirty trees
        if code != 0:
            raise RuntimeError(err)
        note = "Worktree removed (its branch is kept)" + (f"; ignored files saved to {saved}" if saved else "")
        return {"note": note, "recovery": f"git worktree add \"{it['target']}\" {it.get('branch') or it.get('head')}",
                "undo": {"type": "worktree", "repo": repo.path, "path": it["target"], "branch": it.get("branch"),
                         "head": it.get("head"), "ignored_backup": str(saved) if saved else None}}
    if kind == "folder":
        full = os.path.join(repo.path, it["target"])
        if not within(full, repo.path) or os.path.islink(full) or is_junction(full):
            raise RuntimeError("Path is a link or resolves outside the repository; skipped")
        if has_tracked(repo.path, it["target"]):
            raise RuntimeError("Contains tracked files; skipped")
        if permanent:
            if it["status"] != "safe":
                raise RuntimeError("Permanent delete is only allowed for Safe rebuildable folders")
            remove_permanently(full)
            return {"note": "Deleted permanently (rebuildable)", "irreversible": True}
        where = recycle(full)
        return {"note": "Moved to Recycle Bin", "recovery": "Restore it from the Recycle Bin",
                "undo": {"type": "recycled", "path": os.path.abspath(full), "trash": where, "at": now()}}
    if kind == "cache":
        if os.path.islink(it["target"]) or is_junction(it["target"]):
            raise RuntimeError("Cache folder is a link; skipped")
        if permanent and it["status"] == "safe":
            remove_permanently(it["target"])
            return {"note": "Deleted permanently (re-downloadable)", "irreversible": True}
        where = recycle(it["target"])
        return {"note": "Moved to Recycle Bin", "recovery": "Restore it from the Recycle Bin",
                "undo": {"type": "recycled", "path": os.path.abspath(it["target"]), "trash": where, "at": now()}}
    if kind == "gc":
        busy = repo.busy()
        if busy:
            raise RuntimeError("A git operation started meanwhile: " + ", ".join(busy[:3]))
        before = dir_size(repo.git_dir)
        code, _, err = git(repo.path, "gc", "--quiet", timeout=1800)
        after = dir_size(repo.git_dir)
        if code != 0:
            raise RuntimeError(f"git gc failed (the repository is unchanged or only partly packed; git keeps it consistent): {err}")
        freed = max(0, before - after)
        return {"note": f"git gc freed {human(freed)} (estimated up to {human(it['bytes'])})", "bytes": freed, "irreversible": True}
    raise RuntimeError(f"Unknown item kind {kind}")


# ---------------------------------------------------------------- recovery

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _commit_env(repo: Repo) -> dict:
    """An identity for commit-tree when the user has none configured (restored stash entries only)."""
    env = {}
    if not (git_ok(repo.path, "config", "user.name") or "").strip():
        env.update(GIT_AUTHOR_NAME="Repo Pulse", GIT_COMMITTER_NAME="Repo Pulse")
    if not (git_ok(repo.path, "config", "user.email") or "").strip():
        env.update(GIT_AUTHOR_EMAIL="repo-pulse@localhost", GIT_COMMITTER_EMAIL="repo-pulse@localhost")
    return env


def restore_bundle_branches(repo: Repo, bundle: Path, tag: str, only: list[str] | None = None, dry: bool = False) -> list[dict]:
    """Recreate branches that are missing locally. An existing branch is never moved: when its tip differs,
    the backed-up tip is kept at refs/repopulse-backup/<tag>/<name> to compare by hand."""
    code, _, err = git(repo.path, "bundle", "verify", "--quiet", str(bundle), timeout=600)
    if code:
        raise RuntimeError(f"Bundle does not verify against this repository: {err}")
    code, out, err = git(repo.path, "bundle", "list-heads", str(bundle))
    if code:
        raise RuntimeError(err)
    results = []
    for line in out.splitlines():
        sha, _, ref = line.strip().partition(" ")
        source_ref = ref
        if ref == "HEAD":
            name = f"recovered-detached-{sha[:12]}"
            ref = f"refs/heads/{name}"
        elif ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/"):]
        else:
            continue
        if only and name not in only:
            continue
        cur = (git_ok(repo.path, "rev-parse", "--verify", "-q", ref) or "").strip()
        if cur == sha:
            results.append({"branch": name, "done": True, "note": "already present"})
            continue
        if dry:
            results.append({"branch": name, "would": True, "note": "would be recreated" if not cur else
                            "exists with other commits; the backup tip would be kept beside it"})
            continue
        side = f"refs/repopulse-backup/{tag}/{name}"
        code, _, err = git(repo.path, "fetch", "--no-tags", "--quiet", str(bundle), f"{source_ref}:{side}", timeout=600)
        if code:
            results.append({"branch": name, "done": False, "reason": err})
            continue
        if cur:
            results.append({"branch": name, "done": False, "kept_at": side,
                            "reason": f"{name} exists with other commits; the backup tip is at {side}"})
            continue
        code, _, err = git(repo.path, "update-ref", "-m", "repo-pulse: restore", ref, sha, "")  # "" = must not exist
        git(repo.path, "update-ref", "-d", side)
        results.append({"branch": name, "done": code == 0, "note": f"recreated at {sha[:10]}" if code == 0 else None,
                        "reason": None if code == 0 else err})
    return results


def _stash_from_patch(repo: Repo, patch_bytes: bytes, base: str, message: str) -> str:
    """Rebuild a stash entry (w commit with base and index parents) from a backed-up patch."""
    fd, patch = tempfile.mkstemp(prefix="repopulse-stash-", suffix=".patch")
    with os.fdopen(fd, "wb") as fh:
        fh.write(patch_bytes)
    fd, idx = tempfile.mkstemp(prefix="repopulse-index-")
    os.close(fd)
    os.remove(idx)
    env = {"GIT_INDEX_FILE": idx, **_commit_env(repo)}
    try:
        for args in (("read-tree", base), ("apply", "--cached", "--binary", patch)):
            code, _, err = git(repo.path, *args, extra_env=env, timeout=120)
            if code:
                raise RuntimeError(f"could not rebuild the stash: {err}")
        code, tree, err = git(repo.path, "write-tree", extra_env=env)
        if code:
            raise RuntimeError(err)
        code, i_commit, err = git(repo.path, "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "index on " + message, extra_env=env)
        if code:
            raise RuntimeError(err)
        code, w_commit, err = git(repo.path, "commit-tree", tree.strip(), "-p", base, "-p", i_commit.strip(), "-m", message, extra_env=env)
        if code:
            raise RuntimeError(err)
        return w_commit.strip()
    finally:
        for p in (patch, idx, idx + ".lock"):
            try:
                os.remove(p)
            except OSError:
                pass


def restore_stashes(repo: Repo, dest: Path, manifest: dict, tag: str, dry: bool) -> list[dict]:
    existing = set((git_ok(repo.path, "log", "-g", "--format=%H", "refs/stash") or "").split())
    metas = manifest.get("stashes") or []
    patches = manifest.get("stash_patches") or []
    bundle = dest / "unpushed.bundle"
    if not dry and bundle.exists():  # brings the newest stash's exact objects back, if they were lost
        git(repo.path, "fetch", "--no-tags", "--quiet", str(bundle), f"refs/stash:refs/repopulse-backup/{tag}/stash", timeout=600)
    results = []
    for n in reversed(range(len(patches))):  # oldest first, so stash@{0} ends up the newest again
        meta = metas[n] if n < len(metas) else {}
        sha, base = meta.get("sha"), meta.get("base")
        msg = meta.get("message") or f"Restored by Repo Pulse from {patches[n]}"
        if sha and sha in existing:
            results.append({"stash": patches[n], "done": True, "note": "already in the stash list"})
            continue
        if dry:
            results.append({"stash": patches[n], "would": True, "note": "would be added to the stash list"})
            continue
        try:
            if not (sha and git_ok(repo.path, "cat-file", "-e", f"{sha}^{{commit}}") is not None):
                if not base or git_ok(repo.path, "cat-file", "-e", f"{base}^{{commit}}") is None:
                    raise RuntimeError("its base commit is not in this repository; apply the patch by hand")
                sha = _stash_from_patch(repo, read_backup_file(dest / patches[n]), base, msg)
            code, _, err = git(repo.path, "stash", "store", "-m", msg, sha)
            if code:
                raise RuntimeError(err)
            results.append({"stash": patches[n], "done": True, "note": f"added to the stash list as {sha[:10]}"})
        except (RuntimeError, OSError) as exc:
            results.append({"stash": patches[n], "done": False, "reason": str(exc)})
    git(repo.path, "update-ref", "-d", f"refs/repopulse-backup/{tag}/stash")
    return results


def restore_files(root: str, dest: Path, manifest: dict, prefix: str, overwrite: bool, dry: bool) -> list[dict]:
    """Copy backed-up files back. A file with uncommitted changes that differ from the backup is kept
    unless overwrite, and then the current version is saved to pre-restore-<time>/ in the backup first.
    A file with no uncommitted changes is restored over: its current content is still in git."""
    results, pre = [], None
    status = dirty_state(root)
    dirty = None if status.get("error") else {f["path"] for f in status["files"]}
    hashes = manifest.get("sha256") or {}
    keys = sorted(k for k in hashes if k.startswith(prefix + "/"))
    if not hashes and prefix == "files":  # format 1 (0.4 and earlier): no hashes recorded
        keys = [f"files/{f}" for f in manifest.get("files") or [] if isinstance(f, str)]
    for key in keys:
        rel = key[len(prefix) + 1:]
        digest = hashes.get(key)
        target = os.path.join(root, rel)
        if not within(target, root):
            results.append({"path": rel, "done": False, "reason": "points outside the target folder; skipped"})
            continue
        try:
            data = read_backup_file(dest / key)
            digest = digest or hashlib.sha256(data).hexdigest()
            if hashlib.sha256(data).hexdigest() != digest:
                results.append({"path": rel, "done": False, "reason": "the backup copy is damaged (hash mismatch)"})
                continue
            if os.path.lexists(longp(target)):
                if os.path.isfile(longp(target)) and file_sha256(target) == digest:
                    results.append({"path": rel, "done": True, "note": "unchanged"})
                    continue
                committed = dirty is not None and rel not in dirty and \
                    git_ok(root, "ls-files", "--error-unmatch", "--", rel) is not None
                if not overwrite and not committed:
                    results.append({"path": rel, "done": False, "conflict": True,
                                    "reason": "differs from the backup; your version was kept (use --overwrite)"})
                    continue
                if not dry:
                    pre = pre or unique_dir(dest, f"pre-restore-{stamp()}")
                    copy_verified(target, pre / rel)
            if dry:
                results.append({"path": rel, "would": True})
                continue
            os.makedirs(longp(os.path.dirname(target)), exist_ok=True)
            tmp = target + ".repopulse-tmp"
            with open(longp(tmp), "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(longp(tmp), longp(target))
            results.append({"path": rel, "done": True, "note": "restored"})
        except OSError as exc:
            results.append({"path": rel, "done": False, "reason": exc.strerror or str(exc)})
    if pre:
        results.append({"path": str(pre), "done": True, "note": "your previous versions were saved here"})
    return results


def cmd_restore(args) -> dict:
    tag = stamp()
    if args.bundle:
        if not args.repo:
            return {"ok": False, "error": "--bundle needs --repo"}
        repo = Repo(args.repo)
        res = restore_bundle_branches(repo, Path(args.bundle), tag, dry=args.dry_run)
        ok = all(r.get("done") or r.get("would") for r in res)
        if not args.dry_run:
            log_operation(args.dest, {"op": "restore", "op_id": new_op_id(), "repo": repo.path, "bundle": args.bundle, "ok": ok})
        return {"ok": ok, "dry_run": args.dry_run, "branches": res}
    dest = Path(args.backup)
    try:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"No readable manifest.json in {dest}: {exc}"}
    out: dict = {"ok": True, "dry_run": args.dry_run, "backup": str(dest)}
    if not manifest.get("complete", True) or (dest / "INCOMPLETE").exists():
        out["warning"] = "This backup was recorded as incomplete; only what it holds can be restored."
    if manifest.get("kind") == "worktree-ignored":
        root = args.repo or manifest.get("worktree")
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": f"Recreate the worktree first ({manifest.get('worktree')}), or pass --repo"}
        out["files"] = restore_files(root, dest, manifest, "ignored", args.overwrite, args.dry_run)
    else:
        repo = Repo(args.repo or manifest["repo"])
        parts = {p.strip() for p in (args.parts or "files,commits,stashes").split(",") if p.strip()}
        if "files" in parts:
            out["files"] = restore_files(repo.path, dest, manifest, "files", args.overwrite, args.dry_run)
        if "commits" in parts and (dest / "unpushed.bundle").exists():
            out["branches"] = restore_bundle_branches(repo, dest / "unpushed.bundle", tag, dry=args.dry_run)
        if "stashes" in parts and manifest.get("stash_patches"):
            out["stashes"] = restore_stashes(repo, dest, manifest, tag, args.dry_run)
    rows = [r for k in ("files", "branches", "stashes") for r in out.get(k, [])]
    out["ok"] = all(r.get("done") or r.get("would") for r in rows)
    out["conflicts"] = len([r for r in rows if r.get("conflict")])
    if not out["ok"]:
        out["error"] = "; ".join(f"{r.get('path') or r.get('branch') or r.get('stash')}: {r['reason']}"
                                 for r in rows if r.get("reason"))[:600]
    if not args.dry_run:
        log_operation(args.dest, {"op": "restore", "op_id": new_op_id(), "backup": str(dest), "ok": out["ok"],
                                  "conflicts": out["conflicts"]})
    return out


def operations(dest: str | None) -> list[dict]:
    """The journal folded into one row per operation, newest first."""
    rows: dict[str, dict] = {}
    undone = set()
    for rec in read_operations(dest):
        if rec.get("op") == "undo" and rec.get("done"):
            undone.add(rec.get("undoes"))
        oid = rec.get("op_id")
        if not oid:
            continue
        row = rows.setdefault(oid, {})
        row.update(rec)
        if rec.get("phase") == "started":
            row["started_at"] = rec.get("at")
    out = []
    for oid, row in rows.items():
        if row.get("op") == "clean":
            row["state"] = "interrupted" if row.get("phase") == "started" else \
                ("recovered" if row.get("phase") == "recovered" else "done" if row.get("done") else "skipped")
        row["undone"] = oid in undone
        row["undoable"] = bool(row.get("undo")) and row.get("state") == "done" and oid not in undone
        out.append(row)
    return sorted(out, key=lambda r: r.get("at") or "", reverse=True)


def cmd_ops(args) -> dict:
    rows = operations(args.dest)
    return {"ok": True, "operations": rows[:args.limit], "total": len(rows)}


def cmd_undo(args) -> dict:
    row = next((r for r in operations(args.dest) if r.get("op_id") == args.op), None)
    if not row:
        return {"ok": False, "error": f"No operation {args.op} in the journal"}
    if row.get("undone"):
        return {"ok": False, "error": "Already undone"}
    if row.get("state") != "done":
        return {"ok": False, "error": f"The operation is {row.get('state', 'not a cleanup')}; nothing to undo"}
    u = row.get("undo")
    if not u:
        return {"ok": False, "irreversible": True,
                "error": f"Cannot be undone: {row.get('note') or 'this action is irreversible'}"}
    tag = stamp()
    try:
        if u["type"] == "branch":
            res = restore_bundle_branches(Repo(u["repo"]), Path(u["bundle"]), tag, only=[u["name"]])
            if not res or not res[0].get("done"):
                raise RuntimeError(res[0].get("reason") if res else "branch not in the bundle")
            note = f"Branch {u['name']} restored"
        elif u["type"] == "worktree":
            repo = Repo(u["repo"])
            if os.path.lexists(u["path"]):
                raise RuntimeError(f"{u['path']} exists again; nothing was restored")
            checked_out = {t.get("branch") for t in repo.worktrees() or []}
            if u.get("branch") and repo.ref_exists(f"refs/heads/{u['branch']}") and u["branch"] not in checked_out:
                code, _, err = git(repo.path, "worktree", "add", u["path"], u["branch"], timeout=600)
            else:
                code, _, err = git(repo.path, "worktree", "add", "--detach", u["path"], u["head"], timeout=600)
            if code:
                raise RuntimeError(err)
            note = "Worktree recreated"
            if u.get("ignored_backup"):
                bdir = Path(u["ignored_backup"])
                manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
                files = restore_files(u["path"], bdir, manifest, "ignored", False, False)
                bad = [f for f in files if not f.get("done")]
                note += f"; {len(files) - len(bad)} ignored file(s) put back" + (f", {len(bad)} failed" if bad else "")
        elif u["type"] == "recycled":
            unrecycle(u["path"], u.get("trash"))
            note = "Restored from the Recycle Bin"
        else:
            raise RuntimeError(f"Unknown undo type {u['type']}")
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        log_operation(args.dest, {"op": "undo", "undoes": args.op, "done": False, "reason": str(exc)})
        return {"ok": False, "error": str(exc)}
    log_operation(args.dest, {"op": "undo", "undoes": args.op, "done": True, "note": note})
    return {"ok": True, "note": note}


def _interrupted_advice(row: dict, dest: str | None) -> str:
    kind, target = row.get("kind"), row.get("target") or ""
    if kind == "branch":
        try:
            repo = Repo(row["repo"])
        except (RuntimeError, KeyError):
            return "Repository is no longer readable"
        if repo.ref_exists(f"refs/heads/{target}"):
            return "Nothing was lost: the branch still exists"
        bundles = sorted(repo_backup_dir(dest, repo).glob(f"cleanup-*/branch-{safe_name(target)}-*.bundle"),
                         key=lambda p: p.stat().st_mtime)
        return (f"The branch is gone; restore it with: python repopulse.py restore --repo \"{repo.path}\" --bundle \"{bundles[-1]}\""
                if bundles else "The branch is gone and no bundle was found; check git reflog")
    if kind == "worktree":
        return "Nothing was removed: the folder still exists" if os.path.exists(target) else \
            "The worktree was removed; its branch is kept. Ignored files, if any, are in the newest cleanup-* folder"
    if kind in ("folder", "cache"):
        full = os.path.join(row.get("repo") or "", target) if kind == "folder" else target
        return "Nothing was moved: the folder still exists" if os.path.exists(full) else \
            "The folder was moved; look for it in the Recycle Bin"
    if kind == "gc":
        return "git gc keeps the repository consistent even when interrupted; run git fsck to double-check"
    return "Check the target by hand"


def cmd_recover(args) -> dict:
    """Find what a crash, power loss or forced termination left behind, and say how to recover it."""
    rows = operations(args.dest)
    interrupted = [{"op_id": r["op_id"], "kind": r.get("kind"), "target": r.get("target"), "repo": r.get("repo"),
                    "started_at": r.get("started_at"), "advice": _interrupted_advice(r, args.dest)}
                   for r in rows if r.get("state") == "interrupted"]
    root = backup_root(args.dest)
    incomplete = sorted(str(p.parent) for p in root.glob("*/*/INCOMPLETE")) if root.is_dir() else []
    locks = []
    for repo_path in sorted({r["repo"] for r in rows if r.get("repo")}):
        try:
            lock = Path(Repo(repo_path).git_dir) / "repopulse.lock"
            if not lock.exists():
                continue
            pid = int(json.loads(lock.read_text(encoding="utf-8")).get("pid", 0))
        except (RuntimeError, OSError, ValueError, AttributeError):
            continue
        stale = not pid_alive(pid)
        if stale and args.fix:
            lock.unlink(missing_ok=True)
        locks.append({"repo": repo_path, "pid": pid, "stale": stale, "removed": stale and args.fix})
    if args.fix:
        for r in interrupted:
            log_operation(args.dest, {"op": "clean", "op_id": r["op_id"], "phase": "recovered", "advice": r["advice"]})
    return {"ok": True, "interrupted": interrupted, "incomplete_backups": incomplete, "locks": locks,
            "clean": not interrupted and not incomplete and not any(l["stale"] for l in locks)}


def cmd_backups(args) -> dict:
    """Backup management: every backup and cleanup folder, with integrity, size and how to restore it."""
    root = backup_root(args.dest)
    entries = []
    if root.is_dir():
        for repo_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("Trash",)):
            for d in sorted(p for p in repo_dir.iterdir() if p.is_dir()):
                manifest_path = d / "manifest.json"
                if manifest_path.exists():
                    try:
                        m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        m = {}
                    e = {"kind": "backup", "path": str(d), "repo": m.get("repo"), "created": m.get("created"),
                         "complete": bool(m.get("complete", True)) and not (d / "INCOMPLETE").exists(),
                         "encrypted": bool(m.get("encrypted")), "files": len(m.get("files") or []),
                         "has_bundle": (d / "unpushed.bundle").exists(), "stashes": len(m.get("stash_patches") or []),
                         "restore": f"python repopulse.py restore --backup \"{d}\""}
                elif d.name.startswith("cleanup-"):
                    e = {"kind": "cleanup", "path": str(d), "created": d.name[len("cleanup-"):],
                         "bundles": [p.name for p in sorted(d.glob("branch-*.bundle"))],
                         "worktrees": [p.name for p in sorted(d.glob("worktree-*"))], "complete": True}
                else:
                    continue
                e["bytes"] = dir_size(d)
                e["age_days"] = round((now() - d.stat().st_mtime) / 86400, 1)
                if args.verify and e["kind"] == "backup":
                    v = cmd_verify(argparse.Namespace(backup=str(d)))
                    e["verified"] = v["ok"]
                    e["verify_error"] = v.get("error")
                entries.append(e)
    prune = []
    if args.prune_days is not None:  # retention: complete backups older than N days, keeping the newest per repo
        newest: dict[str, str] = {}
        for e in entries:
            if e["kind"] == "backup" and e["complete"]:
                key = os.path.dirname(e["path"])
                newest[key] = max(newest.get(key, ""), e["path"])
        for e in entries:
            if e["age_days"] >= args.prune_days and e["complete"] and newest.get(os.path.dirname(e["path"])) != e["path"]:
                if e.get("verified") is False:
                    continue
                prune.append(e["path"])
        if args.apply:
            for p in prune:
                try:
                    recycle(p)
                    log_operation(args.dest, {"op": "prune-backup", "op_id": new_op_id(), "path": p, "done": True})
                except OSError as exc:
                    log_operation(args.dest, {"op": "prune-backup", "op_id": new_op_id(), "path": p, "done": False, "reason": str(exc)})
    free = free_bytes(root)
    return {"ok": True, "root": str(root), "backups": entries, "free_bytes": free,
            "totals": {"count": len(entries), "bytes": sum(e["bytes"] for e in entries),
                       "incomplete": len([e for e in entries if not e["complete"]])},
            "prune": {"days": args.prune_days, "candidates": prune, "applied": bool(args.apply)} if args.prune_days is not None else None}


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

def render_text(result: dict) -> str:
    """Plain-text report of a scan result, for terminal use (ASCII only)."""
    if not result.get("ok"):
        return "error: " + str(result.get("error"))
    t = result["totals"]
    lines = [f"Repo Pulse {VERSION}: {t['repos']} repos, {t['worktrees']} worktrees, "
             f"{t['needs_attention']} need attention, {human(t['safe_bytes'])} safe / "
             f"{human(t['review_bytes'])} review to reclaim ({result['scan_ms'] / 1000:.1f}s)", ""]
    for r in result["repos"]:
        label = ("  wt " if r.get("kind") == "worktree" else "") + r["name"]
        if r.get("error"):
            lines.append(f"[!] {label}  {r['path']}\n      {r['error']}\n      Resolve access/metadata errors and refresh before acting.")
            continue
        mark = "[!]" if r["attention"] else "[ok]"
        lines.append(f"{mark:4} {label}  ({r.get('branch') or 'detached'})  {r['path']}")
        if r.get("readiness"):
            states = r["readiness"]
            lines.append(f"      risk={r['risk']['level']} resume={states['resume']['state']} "
                         f"sync={states['sync']['state']} clean={states['clean']['state']}")
        lines += [f"      - {n}" for n in r["attention"]]
        lines += [f"      Guidance: {guidance}" for guidance in dict.fromkeys(
            f["guidance"] for f in r.get("risk", {}).get("findings", []))]
        for it in sorted((i for i in r["items"] if i["status"] != "keep"), key=lambda i: -i["bytes"]):
            lines.append(f"      {it['status']:6} {it['kind']:8} {human(it['bytes']):>9}  {it['target']}  [{it['id']}]")
    if result.get("caches"):
        lines += ["", "Global caches:"]
        lines += [f"      {c['status']:6} {c['label']:20} {human(c['bytes']):>9}  {c['target']}  [{c['id']}]" for c in result["caches"]]
    if not result.get("complete", True):
        lines.append("PARTIAL: inspect diagnostics and unknown repositories before making decisions.")
        lines += [f"      {d['path']}: {d['message']}" for d in result.get("diagnostics", [])]
    return "\n".join(lines)


class ContractParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        n = int(value)
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return n
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = ContractParser(prog="repopulse", description="Repo Pulse core")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("scan", "Scan; optionally fetch"), ("snapshot", "Read-only versioned snapshot for Aura")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--roots", help="Semicolon-separated folders to search")
        s.add_argument("--root", action="append", help="Literal scan root (repeatable; supports semicolons in paths)")
        s.add_argument("--repo", action="append", help="Repository path (repeatable; snapshot includes its linked worktrees)")
        s.add_argument("--depth", type=bounded_int(0, 64), default=MAX_DEPTH)
        s.set_defaults(fetch=False)
        if name == "scan":
            s.add_argument("--fetch", action="store_true", help="git fetch --prune first (updates remote-tracking refs only)")
        s.add_argument("--caches", action="store_true", help="Include global package caches")
        s.add_argument("--no-hints", action="store_true", help="Ignore tool-history discovery")
        s.add_argument("--exclude", help="Semicolon-separated folders never to search or report")
        s.add_argument("--exclude-path", action="append", help="Literal excluded folder (repeatable)")
        s.add_argument("--text", action="store_true", help="Print a readable report instead of JSON")
        s.add_argument("--json", action="store_true", help="Print JSON (the default)")
        s.add_argument("--previous", help="Previous snapshot; reuse only bounded-age disk-size estimates")
        s.add_argument("--max-size-age", type=bounded_int(0, 3600), default=300, help="Size-estimate TTL in seconds; 0 forces measurement")
        s.add_argument("--workers", type=bounded_int(1, 16), default=8, help="Bounded concurrent repository reads")
        s.add_argument("--dest", help="Read operation history in this backup root; never create it")
    b = sub.add_parser("backup", help="Copy dirty files and bundle unpushed commits")
    b.add_argument("--repo", required=True)
    b.add_argument("--dest")
    b.add_argument("--max-gb", type=float, default=2.0)
    b.add_argument("--encrypt", action="store_true", help="Encrypt copied files and patches for this Windows account (DPAPI)")
    rs = sub.add_parser("restore", help="Restore files, branches and stashes from a backup, or branches from a bundle")
    rs.add_argument("--backup", help="A backup folder (or a worktree-* folder from a cleanup)")
    rs.add_argument("--bundle", help="A branch bundle from a cleanup folder")
    rs.add_argument("--repo", help="Target repository (default: the one recorded in the backup)")
    rs.add_argument("--parts", help="Comma-separated: files,commits,stashes (default: all)")
    rs.add_argument("--overwrite", action="store_true", help="Replace changed files (your versions are saved first)")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--dest", help="Backup root, for the operation journal")
    u = sub.add_parser("undo", help="Undo one cleanup operation from the journal")
    u.add_argument("--op", required=True, help="Operation id, as listed by `ops`")
    u.add_argument("--dest")
    o = sub.add_parser("ops", help="Operation history: what was done, outcome, recovery")
    o.add_argument("--dest")
    o.add_argument("--limit", type=int, default=100)
    rc = sub.add_parser("recover", help="Find operations cut short by a crash and say how to recover")
    rc.add_argument("--dest")
    rc.add_argument("--fix", action="store_true", help="Remove stale locks and mark interrupted operations as reviewed")
    bk = sub.add_parser("backups", help="List backups with integrity, size and restore commands")
    bk.add_argument("--dest")
    bk.add_argument("--verify", action="store_true", help="Re-hash every backup")
    bk.add_argument("--prune-days", type=float, help="Retention: list complete backups older than N days (newest per repo kept)")
    bk.add_argument("--apply", action="store_true", help="With --prune-days: move them to the Recycle Bin")
    y = sub.add_parser("sync", help="Fast-forward default branch; optional non-force push")
    y.add_argument("--repo", required=True)
    y.add_argument("--push", action="store_true")
    c = sub.add_parser("clean", help="Remove scanned items after re-verification")
    c.add_argument("--repo")
    c.add_argument("--ids", required=True)
    c.add_argument("--review-ids", help="Comma-separated ids the user approved although they are Review")
    c.add_argument("--allow-review", action="store_true", help="Approve every listed id even if it is Review")
    c.add_argument("--permanent", action="store_true", help="Delete Safe rebuildable folders instead of recycling")
    c.add_argument("--dry-run", action="store_true", help="Re-check and report what would happen; change nothing")
    c.add_argument("--encrypt", action="store_true", help="Encrypt the ignored files saved from removed worktrees (DPAPI)")
    c.add_argument("--dest")
    v = sub.add_parser("verify", help="Re-check a backup against its manifest")
    v.add_argument("--backup", required=True)
    f = sub.add_parser("diff", help="Diff text for one file")
    f.add_argument("--repo", required=True)
    f.add_argument("--path", required=True)
    return p


MIN_PYTHON = (3, 11)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stdout.write(json.dumps({"ok": False, "error": f"Repo Pulse needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer "
                                                           f"(this is {sys.version.split()[0]})."}) + "\n")
        return 1
    handlers = {"scan": cmd_scan, "snapshot": cmd_scan, "backup": cmd_backup, "sync": cmd_sync, "clean": cmd_clean, "diff": cmd_diff,
                "verify": cmd_verify, "restore": cmd_restore, "undo": cmd_undo, "ops": cmd_ops,
                "recover": cmd_recover, "backups": cmd_backups}
    args = None
    try:
        args = build_parser().parse_args(argv)
        if getattr(args, "text", False) and getattr(args, "json", False):
            raise ValueError("--text and --json are mutually exclusive")
        result = handlers[args.cmd](args)
    except Exception as exc:
        result = {"ok": False, "schema_version": SCHEMA_VERSION, "complete": False, "error": str(exc)}
    exit_code = 1 if not result.get("ok") else 2 if args and args.cmd == "snapshot" and not result.get("complete") else 0
    if getattr(args, "text", False) and not getattr(args, "json", False):
        sys.stdout.buffer.write(render_text(result).encode(sys.stdout.encoding or "utf-8", "replace") + b"\n")
        sys.stdout.buffer.flush()
        return exit_code
    data = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
