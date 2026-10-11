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
        self.tmp = Path(tempfile.mkdtemp(prefix="repopulse-"))
        os.environ["REPOPULSE_TRASH"] = str(self.tmp / "backups")
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
        bundle = next(Path(res["backup"]).glob("branch-pushed.bundle"))
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
        self.assertEqual([os.path.basename(p) for p in found], ["app"])
        out = subprocess.run([sys.executable, str(Path(rp.__file__)), "scan", "--roots", str(self.tmp / "code")],
                             capture_output=True, env=ENV)
        data = json.loads(out.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["totals"]["repos"], 1)

    def test_broken_repo_does_not_fail_scan(self):
        broken = self.tmp / "code" / "broken"
        (broken / ".git").mkdir(parents=True)
        data = rp.cmd_scan(rp.build_parser().parse_args(["scan", "--roots", str(self.tmp / "code")]))
        self.assertTrue(data["ok"])
        self.assertTrue(any(r.get("error") for r in data["repos"]))


if __name__ == "__main__":
    unittest.main()
