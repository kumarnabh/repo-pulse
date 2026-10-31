# Copyright (c) 2026 Nabh Kumar. MIT License.
"""Fixture tests with real git repositories and a local bare remote.

The core promise under test: nothing that holds unique work is ever classified Safe,
and every action re-verifies before changing anything.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repopulse as rp  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="T",
           GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
os.environ.update({k: ENV[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                                       "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")})


def g(cwd, *args):
    r = subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, capture_output=True, text=True, env=ENV)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def commit(repo, name, text="x"):
    p = Path(repo, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    g(repo, "add", name)
    g(repo, "commit", "-qm", f"add {name}")


def by_target(scan, kind, target):
    for it in scan["items"]:
        if it["kind"] == kind and (it["target"] == target or os.path.normcase(it["target"]) == os.path.normcase(target)):
            return it
    raise AssertionError(f"{kind} {target} not found in {[i['target'] for i in scan['items']]}")


class Fixture(unittest.TestCase):
    def setUp(self):
        # resolve() expands Windows 8.3 short names (RUNNER~1) so paths match what git reports
        self.tmp = Path(tempfile.mkdtemp(prefix="repopulse-")).resolve()
        os.environ["REPOPULSE_TRASH"] = str(self.tmp / "backups")
        rp.ACTIVE_MINUTES = 0  # fixtures are seconds old; TestActiveWorktree turns the in-use check back on
        # isolate tool-history hints (~/.claude.json etc.) from the machine running the tests
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "APPDATA")}
        os.environ.update(HOME=str(self.home), USERPROFILE=str(self.home), APPDATA=str(self.home / "AppData" / "Roaming"))
        self.remote = self.tmp / "remote.git"
        g(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.repo = self.tmp / "code" / "app"
        self.repo.parent.mkdir()
        g(self.repo.parent, "clone", "-q", str(self.remote), "app")
        commit(self.repo, "README.md", "hello")
        g(self.repo, "push", "-q", "-u", "origin", "main")
        g(self.repo, "remote", "set-head", "origin", "main")
        self.dest = self.tmp / "backups"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self):
        g(self.repo, "fetch", "-q", "--prune", "origin")

    def scan(self):
        r = rp.scan_repo(str(self.repo))
        self.assertNotIn("error", r, r.get("error"))
        return r

    def other_clone(self):
        other = self.tmp / "other"
        g(self.tmp, "clone", "-q", str(self.remote), "other")
        return other

    def assert_no_false_safe(self, scan):
        for it in scan["items"]:
            if it["status"] != "safe":
                continue
            if it["kind"] == "branch":
                self.assertEqual(it["unpushed"], 0, it)
                self.assertNotEqual(it["target"], scan["default"], it)
            if it["kind"] == "folder":
                self.assertFalse(rp.has_tracked(str(self.repo), it["target"]), it)


class TestBranches(Fixture):
    def test_classification(self):
        g(self.repo, "switch", "-qc", "merged")
        commit(self.repo, "a.txt")
        g(self.repo, "push", "-q", "-u", "origin", "merged")
        g(self.repo, "switch", "-q", "main")
        g(self.repo, "merge", "-q", "--ff-only", "merged")
        g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "push", "-q", "origin", "--delete", "merged")

        g(self.repo, "switch", "-qc", "pushed")
        commit(self.repo, "p.txt")
        g(self.repo, "push", "-q", "-u", "origin", "pushed")

        g(self.repo, "switch", "-qc", "local", "main")
        commit(self.repo, "l.txt")

        g(self.repo, "switch", "-qc", "squashed", "main")
        commit(self.repo, "s.txt")
        g(self.repo, "push", "-q", "-u", "origin", "squashed")
        g(self.repo, "push", "-q", "origin", "--delete", "squashed")

        g(self.repo, "switch", "-q", "main")
        self.fetch()
        s = self.scan()
        self.assertEqual(by_target(s, "branch", "main")["status"], "keep")
        self.assertEqual(by_target(s, "branch", "merged")["status"], "safe")
        self.assertEqual(by_target(s, "branch", "pushed")["status"], "safe")
        self.assertEqual(by_target(s, "branch", "local")["status"], "keep")
        sq = by_target(s, "branch", "squashed")
        self.assertEqual(sq["status"], "review")
        self.assertTrue(sq["upstream_gone"])
        self.assert_no_false_safe(s)

    def test_stale_fetch_downgrades_to_review(self):
        g(self.repo, "switch", "-qc", "pushed")
        commit(self.repo, "p.txt")
        g(self.repo, "push", "-q", "-u", "origin", "pushed")
        g(self.repo, "switch", "-q", "main")
        self.fetch()
        old = time.time() - 48 * 3600
        os.utime(self.repo / ".git" / "FETCH_HEAD", (old, old))
        self.assertEqual(by_target(self.scan(), "branch", "pushed")["status"], "review")

    def test_never_fetched_is_not_safe(self):
        g(self.repo, "switch", "-qc", "pushed")
        commit(self.repo, "p.txt")
        g(self.repo, "push", "-q", "-u", "origin", "pushed")
        g(self.repo, "switch", "-q", "main")
        fh = self.repo / ".git" / "FETCH_HEAD"
        if fh.exists():
            fh.unlink()
        self.assertEqual(by_target(self.scan(), "branch", "pushed")["status"], "review")

    def test_no_remote_keeps_everything(self):
        solo = self.tmp / "solo"
        solo.mkdir()
        g(solo, "init", "-q")
        commit(solo, "a.txt")
        g(solo, "branch", "side")
        s = rp.scan_repo(str(solo))
        self.assertTrue(all(i["status"] == "keep" for i in s["items"] if i["kind"] == "branch"))
        self.assertEqual(s["main_state"], "no_remote")


class TestWorktrees(Fixture):
    def test_worktree_states(self):
        wts = self.tmp / "wts"
        g(self.repo, "worktree", "add", "-q", str(wts / "clean"), "-b", "wt-clean")
        g(wts / "clean", "push", "-q", "-u", "origin", "wt-clean")
        g(self.repo, "worktree", "add", "-q", str(wts / "dirty"), "-b", "wt-dirty")
        (wts / "dirty" / "README.md").write_text("changed")
        g(self.repo, "worktree", "add", "-q", str(wts / "ahead"), "-b", "wt-ahead")
        commit(wts / "ahead", "ahead.txt")
        g(self.repo, "worktree", "add", "-q", str(wts / "gone"), "-b", "wt-gone")
        shutil.rmtree(wts / "gone")
        g(self.repo, "worktree", "add", "-q", str(wts / "locked"), "-b", "wt-locked")
        g(self.repo, "worktree", "lock", str(wts / "locked"))
        self.fetch()
        s = self.scan()
        st = {Path(i["target"]).name: i["status"] for i in s["items"] if i["kind"] == "worktree"}
        self.assertEqual(st, {"clean": "safe", "dirty": "keep", "ahead": "keep", "gone": "safe", "locked": "keep"})
        # branches checked out in worktrees are never offered
        for b in ("wt-clean", "wt-dirty", "wt-ahead", "wt-locked"):
            self.assertEqual(by_target(s, "branch", b)["status"], "keep")

        ids = [i["id"] for i in s["items"] if i["kind"] == "worktree"]
        args = rp.build_parser().parse_args(["clean", "--repo", str(self.repo), "--ids", ",".join(ids), "--dest", str(self.dest)])
        res = rp.cmd_clean(args)
        done = {Path(r["target"]).name for r in res["results"] if r["done"]}
        self.assertEqual(done, {"clean", "gone"})
        self.assertFalse((wts / "clean").exists())
        self.assertTrue((wts / "dirty" / "README.md").read_text() == "changed")
        self.assertTrue((wts / "ahead").exists())

    def test_ignored_secrets_keep_worktree_out_of_safe(self):
        Path(self.repo, ".gitignore").write_text(".env\nnode_modules/\n")
        Path(self.repo, "package.json").write_text("{}")
        g(self.repo, "add", ".")
        g(self.repo, "commit", "-qm", "ignore")
        g(self.repo, "push", "-q")
        wts = self.tmp / "wts"
        for name in ("secret", "deps"):
            g(self.repo, "worktree", "add", "-q", str(wts / name), "-b", f"wt-{name}")
            g(wts / name, "push", "-q", "-u", "origin", f"wt-{name}")
        (wts / "secret" / ".env").write_text("TOKEN=only-copy")
        (wts / "deps" / "node_modules").mkdir()
        (wts / "deps" / "node_modules" / "x.js").write_text("x")
        self.fetch()
        s = self.scan()
        secret = by_target(s, "worktree", str(wts / "secret"))
        self.assertEqual(secret["status"], "review")
        self.assertIn(".env", secret["reason"])
        self.assertEqual(by_target(s, "worktree", str(wts / "deps"))["status"], "safe")
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", secret["id"], "--dest", str(self.dest)]))
        self.assertFalse(res["results"][0]["done"])
        self.assertEqual((wts / "secret" / ".env").read_text(), "TOKEN=only-copy")

    def test_claude_style_worktree_inside_repo(self):
        inner = self.repo / ".claude" / "worktrees" / "agent-1"
        Path(self.repo, ".gitignore").write_text(".claude/worktrees/\n")
        g(self.repo, "add", ".gitignore")
        g(self.repo, "commit", "-qm", "ignore")
        g(self.repo, "push", "-q")
        g(self.repo, "worktree", "add", "-q", str(inner), "-b", "agent-1")
        g(inner, "push", "-q", "-u", "origin", "agent-1")
        self.fetch()
        s = self.scan()
        self.assertEqual(by_target(s, "worktree", str(inner))["status"], "safe")
        # the nested worktree is not double-counted as a junk folder
        self.assertFalse([i for i in s["items"] if i["kind"] == "folder" and "agent-1" in i["target"]])


class TestFolders(Fixture):
    def setUp(self):
        super().setUp()
        Path(self.repo, ".gitignore").write_text("node_modules/\n.venv/\ndist/\nweb/node_modules/\n")
        Path(self.repo, "package.json").write_text("{}")
        Path(self.repo, "requirements.txt").write_text("")
        g(self.repo, "add", ".")
        g(self.repo, "commit", "-qm", "setup")
        g(self.repo, "push", "-q")

    def mk(self, rel, size=1000):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * size)

    def test_folder_rules(self):
        self.mk("node_modules/lib/index.js", 5000)
        self.mk(".venv/pyvenv.cfg", 10)
        self.mk("dist/app.js")
        self.mk("tools/node_modules/x.js")          # not ignored
        self.mk("web/node_modules/y.js")            # ignored but no package.json in web/
        self.mk("vendor/node_modules/keep.js")      # tracked
        Path(self.repo, ".gitignore").write_text(Path(self.repo, ".gitignore").read_text() + "vendor/node_modules/\n")
        g(self.repo, "add", "-f", "vendor/node_modules/keep.js")
        g(self.repo, "commit", "-qm", "vendor")
        s = self.scan()
        st = {i["target"].replace("\\", "/"): i["status"] for i in s["items"] if i["kind"] == "folder"}
        self.assertEqual(st["node_modules"], "safe")
        self.assertEqual(st[".venv"], "safe")
        self.assertEqual(st["dist"], "review")
        self.assertEqual(st["tools/node_modules"], "review")
        self.assertEqual(st["web/node_modules"], "review")
        self.assertEqual(st["vendor/node_modules"], "keep")
        self.assertGreaterEqual(by_target(s, "folder", "node_modules")["bytes"], 5000)
        self.assert_no_false_safe(s)

        nm = by_target(s, "folder", "node_modules")["id"]
        vendor = by_target(s, "folder", os.path.join("vendor", "node_modules"))["id"]
        dist = by_target(s, "folder", "dist")["id"]
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", f"{nm},{vendor},{dist}", "--dest", str(self.dest)]))
        done = {r["target"] for r in res["results"] if r["done"]}
        self.assertEqual(done, {"node_modules"})
        self.assertFalse((self.repo / "node_modules").exists())
        self.assertTrue((self.repo / "vendor" / "node_modules" / "keep.js").exists())
        self.assertTrue((self.repo / "dist").exists())
        # review items need explicit consent
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", dist, "--allow-review", "--dest", str(self.dest)]))
        self.assertTrue(res["results"][0]["done"])

    def test_tracked_file_added_after_scan_blocks_removal(self):
        self.mk("node_modules/lib/index.js")
        s = self.scan()
        nm = by_target(s, "folder", "node_modules")
        self.assertEqual(nm["status"], "safe")
        g(self.repo, "add", "-f", "node_modules/lib/index.js")
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", nm["id"], "--dest", str(self.dest)]))
        self.assertFalse(res["results"][0]["done"])
        self.assertTrue((self.repo / "node_modules").exists())


class TestStatusSyncBackup(Fixture):
    def test_behind_then_fast_forward(self):
        other = self.other_clone()
        commit(other, "remote.txt")
        g(other, "push", "-q")
        self.fetch()
        s = self.scan()
        self.assertEqual((s["main_state"], s["behind"]), ("behind", 1))
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo)]))
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.repo / "remote.txt").exists())
        self.assertEqual(self.scan()["main_state"], "in_sync")

    def test_ff_when_main_not_checked_out(self):
        other = self.other_clone()
        commit(other, "remote.txt")
        g(other, "push", "-q")
        g(self.repo, "switch", "-qc", "feature")
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo)]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(g(self.repo, "rev-parse", "main"), g(self.repo, "rev-parse", "origin/main"))

    def test_diverged_is_refused(self):
        other = self.other_clone()
        commit(other, "remote.txt")
        g(other, "push", "-q")
        commit(self.repo, "local.txt")
        before = g(self.repo, "rev-parse", "main")
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo), "--push"]))
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "diverged")
        self.assertEqual(g(self.repo, "rev-parse", "main"), before)

    def test_dirty_main_blocks_sync(self):
        other = self.other_clone()
        commit(other, "remote.txt")
        g(other, "push", "-q")
        Path(self.repo, "README.md").write_text("edited")
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo)]))
        self.assertFalse(res["ok"])
        self.assertEqual(Path(self.repo, "README.md").read_text(), "edited")

    def test_push_ahead(self):
        commit(self.repo, "local.txt")
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo)]))
        self.assertTrue(res["ok"])
        self.assertIn("not pushed", res["steps"][0])
        res = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo), "--push"]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(g(self.remote, "rev-parse", "main"), g(self.repo, "rev-parse", "main"))

    def test_dirty_diff_and_backup(self):
        Path(self.repo, "README.md").write_text("hello\nworld\n")
        Path(self.repo, "new dir").mkdir()
        Path(self.repo, "new dir", "n.txt").write_text("new")
        g(self.repo, "switch", "-qc", "wip")
        commit(self.repo, "wip.txt")
        s = self.scan()
        self.assertEqual(s["dirty"]["modified"], 1)
        self.assertEqual(s["dirty"]["untracked"], 1)
        readme = next(f for f in s["dirty"]["files"] if f["path"] == "README.md")
        self.assertEqual(readme["added"], 2)
        self.assertTrue(any("unpushed" in n for n in s["attention"]))
        d = rp.cmd_diff(rp.build_parser().parse_args(["diff", "--repo", str(self.repo), "--path", "README.md"]))
        self.assertIn("+world", d["text"])

        res = rp.cmd_backup(rp.build_parser().parse_args(["backup", "--repo", str(self.repo), "--dest", str(self.dest)]))
        self.assertTrue(res["ok"], res)
        b = Path(res["backup"])
        self.assertEqual((b / "files" / "README.md").read_text(), "hello\nworld\n")
        self.assertTrue((b / "files" / "new dir" / "n.txt").exists())
        self.assertTrue((b / "changes.patch").stat().st_size > 0)
        self.assertTrue(res["bundle"]["ok"])
        g(self.repo, "bundle", "verify", "-q", str(b / "unpushed.bundle"))
        manifest = json.loads((b / "manifest.json").read_text())
        self.assertIn("README.md", manifest["files"])
        # working tree untouched
        self.assertEqual(Path(self.repo, "README.md").read_text(), "hello\nworld\n")

    def test_stash_is_reported_and_bundled(self):
        Path(self.repo, "README.md").write_text("stashed work")
        g(self.repo, "stash", "push", "-q", "-m", "keep me")
        stash_sha = g(self.repo, "rev-parse", "refs/stash")
        s = self.scan()
        self.assertEqual(s["stashes"], 1)
        self.assertTrue(any("stash" in n for n in s["attention"]))
        res = rp.cmd_backup(rp.build_parser().parse_args(["backup", "--repo", str(self.repo), "--dest", str(self.dest)]))
        self.assertTrue(res["bundle"]["ok"], res)
        heads = g(self.repo, "bundle", "list-heads", str(Path(res["backup"]) / "unpushed.bundle"))
        self.assertIn(stash_sha, heads)

    def test_every_stash_is_saved(self):
        for text in ("first", "second", "third"):
            Path(self.repo, "README.md").write_text(text)
            g(self.repo, "stash", "push", "-q", "-m", text)
        res = rp.cmd_backup(rp.build_parser().parse_args(["backup", "--repo", str(self.repo), "--dest", str(self.dest)]))
        self.assertEqual(res["stash_patches"], 3)
        b = Path(res["backup"])
        self.assertIn("+first", (b / "stash-2.patch").read_text())
        self.assertIn("+third", (b / "stash-0.patch").read_text())
        # the oldest stash restores from its patch
        g(self.repo, "apply", "--binary", str(b / "stash-2.patch"))
        self.assertEqual(Path(self.repo, "README.md").read_text(), "first")

    def test_renamed_file_in_folder_gets_line_counts(self):
        body = "".join(f"line {n}\n" for n in range(20))
        commit(self.repo, "src/old/mod.py", body)
        g(self.repo, "mv", "src/old", "src/new")
        Path(self.repo, "src", "new", "mod.py").write_text(body + "extra\n")
        g(self.repo, "add", "-A")
        stats = rp.numstat(rp.Repo(str(self.repo)))
        self.assertEqual(stats.get("src/new/mod.py"), (1, 0))  # git reports this as src/{old => new}/mod.py
        self.assertNotIn("src/old/mod.py", stats)

    def test_backup_size_limit(self):
        Path(self.repo, "big.bin").write_bytes(b"0" * 2_000_000)
        res = rp.cmd_backup(rp.build_parser().parse_args(
            ["backup", "--repo", str(self.repo), "--dest", str(self.dest), "--max-gb", "0.001"]))
        self.assertFalse(res["ok"])
        self.assertFalse(self.dest.exists())


class TestCleanBranches(Fixture):
    def test_delete_bundles_and_restores(self):
        g(self.repo, "switch", "-qc", "pushed")
        commit(self.repo, "p.txt")
        g(self.repo, "push", "-q", "-u", "origin", "pushed")
        g(self.repo, "switch", "-q", "main")
        tip = g(self.repo, "rev-parse", "pushed")
        self.fetch()
        it = by_target(self.scan(), "branch", "pushed")
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", it["id"], "--dest", str(self.dest)]))
        self.assertTrue(res["results"][0]["done"], res)
        self.assertNotIn("pushed", g(self.repo, "branch", "--list", "pushed"))
        bundle = next(Path(res["backup"]).glob("branch-pushed-*.bundle"))
        g(self.repo, "fetch", "-q", str(bundle), "pushed:pushed")
        self.assertEqual(g(self.repo, "rev-parse", "pushed"), tip)

    def test_commit_after_scan_blocks_delete(self):
        g(self.repo, "switch", "-qc", "pushed")
        commit(self.repo, "p.txt")
        g(self.repo, "push", "-q", "-u", "origin", "pushed")
        self.fetch()
        g(self.repo, "switch", "-q", "main")
        it = by_target(self.scan(), "branch", "pushed")
        self.assertEqual(it["status"], "safe")
        g(self.repo, "switch", "-q", "pushed")
        commit(self.repo, "late.txt")
        g(self.repo, "switch", "-q", "main")
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", it["id"], "--dest", str(self.dest)]))
        self.assertFalse(res["results"][0]["done"])
        self.assertIn("pushed", g(self.repo, "branch", "--list", "pushed"))

    def test_keep_items_never_execute_even_with_allow_review(self):
        g(self.repo, "switch", "-qc", "local")
        commit(self.repo, "l.txt")
        g(self.repo, "switch", "-q", "main")
        self.fetch()
        it = by_target(self.scan(), "branch", "local")
        res = rp.cmd_clean(rp.build_parser().parse_args(
            ["clean", "--repo", str(self.repo), "--ids", it["id"], "--allow-review", "--dest", str(self.dest)]))
        self.assertFalse(res["results"][0]["done"])


class TestDiscoveryAndCli(Fixture):
    def test_discover_and_full_scan_json(self):
        g(self.repo, "worktree", "add", "-q", str(self.tmp / "code" / "wt"), "-b", "wt")
        Path(self.tmp, "code", "app", "node_modules").mkdir()
        found = rp.discover([str(self.tmp / "code")])
        self.assertEqual([(os.path.basename(e["path"]), e["kind"]) for e in found], [("app", "repo"), ("wt", "worktree")])
        # the child process must not read the host's tool history either (APPDATA holds VS Code's)
        env = dict(ENV, HOME=str(self.home), USERPROFILE=str(self.home), APPDATA=os.environ["APPDATA"])
        out = subprocess.run([sys.executable, str(Path(rp.__file__)), "scan", "--roots", str(self.tmp / "code")],
                             capture_output=True, env=env)
        data = json.loads(out.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual((data["totals"]["repos"], data["totals"]["worktrees"]), (1, 1))
        text = subprocess.run([sys.executable, str(Path(rp.__file__)), "scan", "--roots", str(self.tmp / "code"), "--text"],
                              capture_output=True, env=env)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn(b"1 repos, 1 worktrees", text.stdout)

    def test_exclude(self):
        found = rp.discover([str(self.tmp / "code")], exclude=[str(self.repo)])
        self.assertEqual(found, [])
        g(self.repo, "worktree", "add", "-q", str(self.tmp / "code" / "wt"), "-b", "wt")
        found = rp.discover([str(self.tmp / "code")], exclude=[str(self.tmp / "code" / "wt")])
        self.assertEqual([os.path.basename(e["path"]) for e in found], ["app"])

    def test_fetch_scan_of_local_only_repo_is_not_an_error(self):
        solo = self.tmp / "code" / "solo"
        solo.mkdir()
        g(solo, "init", "-q")
        commit(solo, "a.txt")
        data = rp.cmd_scan(rp.build_parser().parse_args(["scan", "--roots", str(self.tmp / "code"), "--fetch", "--no-hints"]))
        r = next(r for r in data["repos"] if r["name"] == "solo")
        self.assertFalse(r.get("fetch_error"))
        self.assertFalse(any("Fetch failed" in n for n in r["attention"]))

    def test_more_tool_history(self):
        jb_proj, vs_proj, parent_only = self.tmp / "jb-proj", self.tmp / "vs-recent", self.tmp / "parent-only"
        for p in (jb_proj, vs_proj, parent_only):
            p.mkdir()
            g(p, "init", "-q")
        jb = Path(os.environ["APPDATA"]) / "JetBrains" / "PyCharm2026.2" / "options"
        jb.mkdir(parents=True)
        (jb / "recentProjects.xml").write_text(
            f'<application><component name="RecentProjectsManager"><option name="additionalInfo"><map>'
            f'<entry key="{jb_proj.as_posix()}"><value/></entry>'
            f'<entry key="$USER_HOME$/missing"><value/></entry></map></option></component></application>')
        import sqlite3
        storage = Path(os.environ["APPDATA"]) / "Cursor" / "User" / "globalStorage"
        storage.mkdir(parents=True)
        con = sqlite3.connect(storage / "state.vscdb")
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES ('history.recentlyOpenedPathsList', ?)",
                    (json.dumps({"entries": [{"folderUri": vs_proj.as_uri()}, {"folderUri": (parent_only / "deleted").as_uri()}]}),))
        con.commit()
        con.close()
        found = {os.path.basename(e["path"]): e["source"] for e in rp.discover([str(self.tmp / "code")])}
        self.assertEqual(found.get("jb-proj"), "JetBrains")
        self.assertEqual(found.get("vs-recent"), "Cursor")
        self.assertNotIn("parent-only", found)  # a deleted folder never resolves to its parent repo

    def test_worktree_outside_roots_and_submodule(self):
        agent = self.tmp / "elsewhere" / ".codex" / "worktrees" / "task-1"
        g(self.repo, "worktree", "add", "-q", str(agent), "-b", "task-1")
        lib = self.tmp / "lib"
        lib.mkdir()
        g(lib, "init", "-q")
        commit(lib, "lib.txt")
        g(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        found = {os.path.normcase(e["path"]): e for e in rp.discover([str(self.tmp / "code")])}
        self.assertIn(os.path.normcase(str(agent)), found)
        self.assertEqual(found[os.path.normcase(str(agent))]["source"], "worktree list")
        self.assertNotIn(os.path.normcase(str(self.repo / "vendor" / "lib")), found)

    def test_tool_history_hints(self):
        outside = self.tmp / "D-drive"
        for name in ("claude-proj", "codex-proj", "vscode-proj"):
            (outside / name).mkdir(parents=True)
            g(outside / name, "init", "-q")
            commit(outside / name, "a.txt")
        (outside / "claude-proj" / "src").mkdir()
        (self.home / ".claude.json").write_text(json.dumps({"projects": {str(outside / "claude-proj" / "src"): {}}}))
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text(f"[projects.'{outside / 'codex-proj'}']\ntrust_level = \"trusted\"\n")
        storage = Path(os.environ["APPDATA"]) / "Code" / "User" / "globalStorage"  # setUp sets APPDATA on every OS
        storage.mkdir(parents=True)
        (storage / "storage.json").write_text(json.dumps({"backupWorkspaces": {"folders": [{"folderUri": (outside / "vscode-proj").as_uri()}]}}))
        found = {os.path.basename(e["path"]): e["source"] for e in rp.discover([str(self.tmp / "code")])}
        self.assertEqual(found.get("claude-proj"), "Claude Code")
        self.assertEqual(found.get("codex-proj"), "Codex")
        self.assertEqual(found.get("vscode-proj"), "Code")
        self.assertNotIn("claude-proj", {os.path.basename(e["path"]) for e in rp.discover([str(self.tmp / "code")], hints=False)})

    def test_worktree_row(self):
        wt = self.tmp / "wt-feature"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        commit(wt, "f.txt")
        Path(wt, ".gitignore").write_text("node_modules/\n")
        Path(wt, "package.json").write_text("{}")
        (wt / "node_modules").mkdir()
        (wt / "node_modules" / "x.js").write_text("x" * 3000)
        r = rp.scan_repo(str(wt))
        self.assertEqual(r["kind"], "worktree")
        self.assertEqual(os.path.normcase(r["parent"]), os.path.normcase(str(self.repo)))
        self.assertEqual(r["branch"], "feature")
        self.assertEqual(r["branch_ahead"], 1)
        self.assertTrue(any("feature has 1 unpushed" in n for n in r["attention"]))
        self.assertEqual({i["kind"] for i in r["items"]}, {"folder"})
        self.assertTrue(any(i["target"] == "node_modules" for i in r["items"]))
        self.assertEqual(r["git_bytes"], 0)

    def test_branch_behind_upstream(self):
        g(self.repo, "switch", "-qc", "topic")
        commit(self.repo, "t.txt")
        g(self.repo, "push", "-q", "-u", "origin", "topic")
        g(self.repo, "reset", "-q", "--hard", "HEAD~1")
        r = self.scan()
        self.assertEqual((r["branch_ahead"], r["branch_behind"]), (0, 1))
        self.assertTrue(any("topic is 1 behind" in n for n in r["attention"]))

    def test_broken_repo_does_not_fail_scan(self):
        broken = self.tmp / "code" / "broken"
        (broken / ".git").mkdir(parents=True)
        data = rp.cmd_scan(rp.build_parser().parse_args(["scan", "--roots", str(self.tmp / "code")]))
        self.assertTrue(data["ok"])
        self.assertTrue(any(r.get("error") for r in data["repos"]))


def clean(*argv):
    return rp.cmd_clean(rp.build_parser().parse_args(["clean", *argv]))


def backup(*argv):
    return rp.cmd_backup(rp.build_parser().parse_args(["backup", *argv]))


class TestReleaseSafety(Fixture):
    """Regressions for the 0.5 safety release: backups, approvals, races and recovery."""

    def pushed_branch(self, name, file=None):
        g(self.repo, "switch", "-qc", name, "main")
        commit(self.repo, file or f"{name.replace('/', '-')}.txt")
        g(self.repo, "push", "-q", "-u", "origin", name)
        g(self.repo, "switch", "-q", "main")
        return g(self.repo, "rev-parse", name)

    def test_similar_branch_names_get_separate_bundles(self):
        tips = {n: self.pushed_branch(n) for n in ("feat/x", "feat_x")}
        self.fetch()
        s = self.scan()
        ids = [by_target(s, "branch", n)["id"] for n in tips]
        res = clean("--repo", str(self.repo), "--ids", ",".join(ids), "--dest", str(self.dest))
        self.assertTrue(all(r["done"] for r in res["results"]), res)
        bundles = sorted(Path(res["backup"]).glob("branch-feat_x-*.bundle"))
        self.assertEqual(len(bundles), 2)
        for name, tip in tips.items():
            r = next(r for r in res["results"] if r["target"] == name)
            g(self.repo, "fetch", "-q", r["recovery"].split('"')[1], f"{name}:{name}")
            self.assertEqual(g(self.repo, "rev-parse", name), tip)

    def test_backup_folders_never_collide(self):
        twin = self.tmp / "elsewhere" / "app"
        twin.parent.mkdir()
        g(twin.parent, "clone", "-q", str(self.remote), "app")
        for repo in (self.repo, twin):
            Path(repo, "README.md").write_text(f"edited in {repo.parent.name}")
        a = backup("--repo", str(self.repo), "--dest", str(self.dest))
        b = backup("--repo", str(twin), "--dest", str(self.dest))
        c = backup("--repo", str(self.repo), "--dest", str(self.dest))  # same repo, same second
        self.assertEqual(len({a["backup"], b["backup"], c["backup"]}), 3)
        self.assertEqual((Path(b["backup"]) / "files" / "README.md").read_text(), "edited in elsewhere")
        self.assertEqual((Path(a["backup"]) / "files" / "README.md").read_text(), "edited in code")

    def test_failed_copy_makes_backup_incomplete(self):
        Path(self.repo, "README.md").write_text("changed")
        Path(self.repo, "locked.txt").write_text("in use")
        real = rp.copy_verified

        def flaky(src, dst):
            if src.endswith("locked.txt"):
                raise PermissionError(13, "The process cannot access the file")
            return real(src, dst)
        rp.copy_verified = flaky
        try:
            res = backup("--repo", str(self.repo), "--dest", str(self.dest))
        finally:
            rp.copy_verified = real
        self.assertFalse(res["ok"])
        self.assertFalse(res["complete"])
        self.assertIn("INCOMPLETE", res["error"])
        self.assertEqual([f["path"] for f in res["failed"]], ["locked.txt"])
        self.assertTrue((Path(res["backup"]) / "INCOMPLETE").exists())
        self.assertTrue((Path(res["backup"]) / "files" / "README.md").exists())  # the rest is still saved

    def test_backup_is_hashed_restorable_and_verifiable(self):
        Path(self.repo, "README.md").write_text("hello\nworld\n")
        g(self.repo, "add", "README.md")
        Path(self.repo, "README.md").write_text("hello\nworld\nagain\n")
        Path(self.repo, "new", "deep").mkdir(parents=True)
        Path(self.repo, "new", "deep", "n.txt").write_text("new")
        res = backup("--repo", str(self.repo), "--dest", str(self.dest))
        self.assertTrue(res["ok"], res)
        b = Path(res["backup"])
        self.assertFalse((b / "INCOMPLETE").exists())
        manifest = json.loads((b / "manifest.json").read_text())
        self.assertIn("files/new/deep/n.txt", manifest["sha256"])
        self.assertIn("staged.patch", manifest["sha256"])
        v = rp.cmd_verify(rp.build_parser().parse_args(["verify", "--backup", str(b)]))
        self.assertTrue(v["ok"], v)
        (b / "files" / "README.md").write_text("tampered")
        v = rp.cmd_verify(rp.build_parser().parse_args(["verify", "--backup", str(b)]))
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad"][0]["path"], "files/README.md")

    def test_ignored_files_are_saved_before_worktree_removal(self):
        Path(self.repo, ".gitignore").write_text(".env\nlocal/\n")
        g(self.repo, "add", ".gitignore")
        g(self.repo, "commit", "-qm", "ignore")
        g(self.repo, "push", "-q")
        wt = self.tmp / "wts" / "secret"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "wt-secret")
        g(wt, "push", "-q", "-u", "origin", "wt-secret")
        (wt / ".env").write_text("TOKEN=only-copy")
        (wt / "local").mkdir()
        (wt / "local" / "db.sqlite").write_bytes(b"\x00data")
        self.fetch()
        it = by_target(self.scan(), "worktree", str(wt))
        self.assertEqual(it["status"], "review")
        res = clean("--repo", str(self.repo), "--ids", it["id"], "--review-ids", it["id"], "--dest", str(self.dest))
        self.assertTrue(res["results"][0]["done"], res)
        self.assertFalse(wt.exists())
        saved = next(Path(res["backup"]).glob("worktree-secret-*"))
        self.assertEqual((saved / "ignored" / ".env").read_text(), "TOKEN=only-copy")
        self.assertEqual((saved / "ignored" / "local" / "db.sqlite").read_bytes(), b"\x00data")

    def test_item_that_became_review_is_not_removed_with_other_review_items(self):
        Path(self.repo, ".gitignore").write_text("dist/\n")
        g(self.repo, "add", ".gitignore")
        g(self.repo, "commit", "-qm", "ignore")
        g(self.repo, "push", "-q")
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "a.js").write_text("x")
        self.pushed_branch("done")
        self.fetch()
        s = self.scan()
        branch, dist = by_target(s, "branch", "done"), by_target(s, "folder", "dist")
        self.assertEqual((branch["status"], dist["status"]), ("safe", "review"))
        old = time.time() - 48 * 3600  # fetch goes stale between selection and cleanup: branch is Review now
        os.utime(self.repo / ".git" / "FETCH_HEAD", (old, old))
        res = clean("--repo", str(self.repo), "--ids", f"{branch['id']},{dist['id']}", "--review-ids", dist["id"],
                    "--dest", str(self.dest))
        by = {r["target"]: r for r in res["results"]}
        self.assertTrue(by["dist"]["done"])
        self.assertFalse(by["done"]["done"])
        self.assertIn("select it again", by["done"]["reason"])
        self.assertIn("done", g(self.repo, "branch", "--list", "done"))

    def test_branch_moved_by_another_tool_is_not_deleted(self):
        self.pushed_branch("racing")
        self.fetch()
        repo = rp.Repo(str(self.repo))
        it = rp.revalidate(str(self.repo), by_target(self.scan(), "branch", "racing"))
        self.assertEqual(it["status"], "safe")
        g(self.repo, "switch", "-q", "racing")  # an agent commits right after the last check
        commit(self.repo, "agent.txt")
        g(self.repo, "switch", "-q", "main")
        moved = g(self.repo, "rev-parse", "racing")
        with self.assertRaises(RuntimeError) as ctx:
            rp.execute(repo, it, lambda: rp.unique_dir(self.dest, "race"), False)
        self.assertIn("changed since it was checked", str(ctx.exception))
        self.assertEqual(g(self.repo, "rev-parse", "racing"), moved)

    def test_branch_being_rebased_is_kept(self):
        self.pushed_branch("topic")
        self.fetch()
        rb = self.repo / ".git" / "rebase-merge"
        rb.mkdir()
        (rb / "head-name").write_text("refs/heads/topic\n")
        self.assertEqual(by_target(self.scan(), "branch", "topic")["reason"], "Being rebased")

    def test_merged_and_pushed_are_told_apart(self):
        self.pushed_branch("only-pushed")
        self.pushed_branch("release/1.0")
        self.fetch()
        s = self.scan()
        it = by_target(s, "branch", "only-pushed")
        self.assertEqual((it["status"], it["merged"]), ("safe", False))
        self.assertIn("not merged", it["reason"])
        self.assertEqual(by_target(s, "branch", "release/1.0")["status"], "review")
        g(self.repo, "merge", "-q", "--ff-only", "only-pushed")
        g(self.repo, "push", "-q")
        self.fetch()
        it = by_target(self.scan(), "branch", "only-pushed")
        self.assertTrue(it["merged"])
        self.assertIn("Merged into origin/main", it["reason"])

    def test_precise_prune_leaves_other_stale_worktrees(self):
        wts = self.tmp / "wts"
        for n in ("one", "two"):
            g(self.repo, "worktree", "add", "-q", str(wts / n), "-b", f"wt-{n}")
            shutil.rmtree(wts / n)
        s = self.scan()
        one = by_target(s, "worktree", str(wts / "one"))
        res = clean("--repo", str(self.repo), "--ids", one["id"], "--dest", str(self.dest))
        self.assertTrue(res["results"][0]["done"], res)
        listed = [l[9:].replace("\\", "/") for l in g(self.repo, "worktree", "list", "--porcelain").splitlines()
                  if l.startswith("worktree ")]
        self.assertFalse(any(p.endswith("/one") for p in listed))
        self.assertTrue(any(p.endswith("/two") for p in listed))

    def test_stale_detached_worktree_with_unique_commits_is_kept(self):
        wt = self.tmp / "wts" / "detached"
        g(self.repo, "worktree", "add", "-q", "--detach", str(wt))
        commit(wt, "only-here.txt")
        shutil.rmtree(wt)
        it = by_target(self.scan(), "worktree", str(wt))
        self.assertEqual(it["status"], "keep")
        self.assertIn("orphan", it["reason"])

    def test_dry_run_changes_nothing(self):
        self.pushed_branch("old")
        self.fetch()
        it = by_target(self.scan(), "branch", "old")
        res = clean("--repo", str(self.repo), "--ids", it["id"], "--dry-run", "--dest", str(self.dest))
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["results"][0]["would"])
        self.assertFalse(res["results"][0]["done"])
        self.assertIn("old", g(self.repo, "branch", "--list", "old"))
        self.assertFalse(self.dest.exists())

    def test_concurrent_cleanup_is_refused(self):
        self.pushed_branch("old")
        self.fetch()
        it = by_target(self.scan(), "branch", "old")
        lock = rp.acquire_lock(rp.Repo(str(self.repo)))
        try:
            res = clean("--repo", str(self.repo), "--ids", it["id"], "--dest", str(self.dest))
        finally:
            lock.unlink()
        self.assertFalse(res["ok"])
        self.assertIn("Another Repo Pulse cleanup", res["error"])
        res = clean("--repo", str(self.repo), "--ids", it["id"], "--dest", str(self.dest))
        self.assertTrue(res["results"][0]["done"], res)
        history = (self.dest / "operations.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(history[-1])["target"], "old")

    def test_gc_is_held_while_git_is_busy(self):
        rp.GC_THRESHOLD, saved = 0, rp.GC_THRESHOLD
        try:
            Path(self.repo, "loose.txt").write_text("loose object")
            g(self.repo, "add", "loose.txt")
            (self.repo / ".git" / "MERGE_HEAD").write_text(g(self.repo, "rev-parse", "HEAD"))
            self.assertEqual(rp.gc_item(rp.Repo(str(self.repo)))["status"], "keep")
            (self.repo / ".git" / "MERGE_HEAD").unlink()
            it = by_target(self.scan(), "gc", "git gc")
            res = clean("--repo", str(self.repo), "--ids", it["id"], "--dest", str(self.dest))
            self.assertTrue(res["results"][0]["done"], res)
            self.assertIn("estimated", res["results"][0]["note"])
        finally:
            rp.GC_THRESHOLD = saved


class TestEngineAccuracy(Fixture):
    def test_failed_fetch_is_not_fresh(self):
        r = rp.Repo(str(self.repo))
        self.assertIsNone(r.fetch())
        self.assertTrue(r.fresh())
        # last success two days ago; then a fetch fails but still rewrites FETCH_HEAD (partial failure)
        state_path = Path(r.git_dir, "repopulse-fetch.json")
        state = json.loads(state_path.read_text())
        state["origin"]["ok"] = time.time() - 48 * 3600
        state_path.write_text(json.dumps(state))
        g(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertIsNotNone(r.fetch())
        failed_at = json.loads(state_path.read_text())["origin"]["failed"]
        os.utime(Path(r.git_dir, "FETCH_HEAD"), (failed_at - 1, failed_at - 1))
        self.assertFalse(rp.Repo(str(self.repo)).fresh())
        # another tool fetching successfully afterwards makes it fresh again
        later = failed_at + 5
        os.utime(Path(r.git_dir, "FETCH_HEAD"), (later, later))
        self.assertTrue(rp.Repo(str(self.repo)).fresh())

    def test_trunk_default_branch(self):
        remote = self.tmp / "trunk.git"
        g(self.tmp, "init", "-q", "--bare", "--initial-branch=trunk", str(remote))
        repo = self.tmp / "t"
        g(self.tmp, "clone", "-q", str(remote), "t")
        g(repo, "switch", "-qc", "trunk")
        commit(repo, "a.txt")
        g(repo, "push", "-q", "-u", "origin", "trunk")
        self.assertEqual(rp.Repo(str(repo)).default, "trunk")
        g(repo, "switch", "-qc", "side")
        s = rp.scan_repo(str(repo))
        self.assertEqual(s["default"], "trunk")
        self.assertEqual(by_target(s, "branch", "trunk")["status"], "keep")

    def test_git_failures_show_as_unknown(self):
        real = rp.git_ok
        rp.git_ok = lambda repo, *a, **k: None if a[:2] == ("stash", "list") else real(repo, *a, **k)
        try:
            s = self.scan()
        finally:
            rp.git_ok = real
        self.assertEqual(s["health"], "unknown")
        self.assertIn("Could not list stashes", s["attention"])

    def test_untracked_folders_list_their_files(self):
        Path(self.repo, "new", "a").mkdir(parents=True)
        Path(self.repo, "new", "a", "1.txt").write_text("1")
        Path(self.repo, "new", "2.txt").write_text("2")
        s = self.scan()
        self.assertEqual(s["dirty"]["untracked"], 2)
        self.assertEqual(sorted(f["path"] for f in s["dirty"]["files"]), ["new/2.txt", "new/a/1.txt"])


class TestActiveWorktree(Fixture):
    def test_recently_used_worktree_is_kept(self):
        rp.ACTIVE_MINUTES = 30
        wt = self.tmp / "wts" / "agent"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "agent")
        g(wt, "push", "-q", "-u", "origin", "agent")
        self.fetch()
        it = by_target(self.scan(), "worktree", str(wt))
        self.assertEqual(it["status"], "keep")
        self.assertIn("coding agent", it["reason"])
        old = time.time() - 3600
        for root, dirs, files in os.walk(wt):
            for f in files:
                os.utime(os.path.join(root, f), (old, old))
        admin = rp.worktree_admin_dir(rp.Repo(str(self.repo)), str(wt))
        for f in ("index", "HEAD", "logs/HEAD"):
            if os.path.exists(os.path.join(admin, f)):
                os.utime(os.path.join(admin, f), (old, old))
        self.assertEqual(by_target(self.scan(), "worktree", str(wt))["status"], "safe")


if __name__ == "__main__":
    unittest.main()
