"""Versioned Aura contract and live-state regressions, using real Git fixtures."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_repopulse import Fixture, by_target, commit, g, rp

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None


class TestSnapshot(Fixture):
    def snapshot(self, *extra):
        return rp.cmd_scan(rp.build_parser().parse_args([
            "snapshot", "--repo", str(self.repo), "--no-hints", "--dest", str(self.dest), *extra]))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(Path(rp.__file__)), *args],
                              capture_output=True, env=os.environ.copy(), timeout=120)

    def test_contract_stable_ids_and_read_only_bytes(self):
        self.fetch()
        commit(self.repo, "staged ü.txt")
        (self.repo / "README.md").write_text("unfinished")
        g(self.repo, "stash", "push", "-qm", "preserve")
        (self.repo / "untracked ü.txt").write_text("local")
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.repo.rglob("*") if p.is_file()}
        data = self.snapshot()
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.dest.exists())
        self.assertTrue(data["complete"], data)
        self.assertEqual(data["schema_version"], "1.0")
        self.assertTrue(data["read_only"])
        row = data["repos"][0]
        self.assertEqual(row["dirty"]["untracked"], 1)
        self.assertEqual(row["stashes"], 1)
        self.assertEqual(row["branches"][0]["unpushed"], 1)
        self.assertEqual(row["readiness"]["resume"]["state"], "safe")
        self.assertEqual(row["readiness"]["sync"]["state"], "review")
        again = self.snapshot()["repos"][0]
        self.assertEqual((row["id"], row["repository_id"]), (again["id"], again["repository_id"]))

    def test_cli_partial_exit_and_json_errors(self):
        broken = self.repo.parent / "broken"
        (broken / ".git").mkdir(parents=True)
        process = self.cli("snapshot", "--root", str(self.repo.parent), "--no-hints", "--dest", str(self.dest))
        self.assertEqual(process.returncode, 2, process.stderr)
        data = json.loads(process.stdout)
        self.assertFalse(data["complete"])
        row = next(r for r in data["repos"] if r["path"] == str(broken))
        self.assertEqual(row["risk"]["level"], "unknown")
        self.assertEqual(row["readiness"]["clean"]["safe_item_ids"], [])
        self.assertGreaterEqual(data["totals"]["needs_attention"], 1)
        for args in (("snapshot", "--fetch"), ("snapshot", "--workers", "0"),
                     ("snapshot", "--text", "--json"), ("snapshot", "--permanent")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])

    @unittest.skipIf(Draft202012Validator is None, "Install requirements-dev.txt for JSON Schema validation")
    def test_published_json_schema(self):
        schema = json.loads((Path(rp.__file__).parent / "docs" / "snapshot.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(self.snapshot())
        broken = self.repo.parent / "broken"
        (broken / ".git").mkdir(parents=True)
        partial = rp.snapshot(roots=[str(self.repo.parent)], backup_dir=str(self.dest))
        validator.validate(partial)
        validator.validate({"ok": False, "schema_version": "1.0", "complete": False, "error": "bad args"})
        with patch.object(rp, "dirty_state", return_value={**rp.parse_status(""), "error": "unreadable"}):
            validator.validate(self.snapshot())

    def test_missing_and_excluded_roots(self):
        missing = self.tmp / "gone"
        data = rp.snapshot(roots=[str(missing)], backup_dir=str(self.dest))
        self.assertFalse(data["complete"])
        self.assertEqual(data["diagnostics"][0]["code"], "root_unavailable")
        with patch.object(rp.os, "scandir", side_effect=AssertionError("Excluded root was walked")):
            self.assertEqual(rp.discover([str(self.repo)], hints=False, exclude=[str(self.repo)]), [])

    def test_literal_unusual_paths_and_linked_owner_discovery(self):
        wt = self.tmp / ".codex" / "worktrees" / "space ü; dollar $ [brackets]"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "agent")
        data = rp.snapshot(roots=[str(wt)], backup_dir=str(self.dest))
        self.assertEqual((data["totals"]["repos"], data["totals"]["worktrees"]), (1, 1))
        rows = {rp._norm_key(r["path"]): r for r in data["repos"]}
        main, linked = rows[rp._norm_key(str(self.repo))], rows[rp._norm_key(str(wt))]
        self.assertEqual(main["repository_id"], linked["repository_id"])
        self.assertNotEqual(main["id"], linked["id"])
        self.assertEqual(linked["agent_origin"], "codex")
        self.assertEqual(rp._norm_key(linked["parent"]), rp._norm_key(str(self.repo)))
        self.assertEqual(main["worktree_inventory"][1]["orphan_status"], "candidate")

    @unittest.skipIf(os.name == "nt", "Windows forbids newlines in file names")
    def test_worktree_newline_and_quote_path(self):
        wt = self.tmp / 'line\nwith\t"quote'
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "odd")
        data = self.snapshot()
        self.assertIn(str(wt), [r["path"] for r in data["repos"]])
        item = by_target(next(r for r in data["repos"] if r["kind"] == "repo"), "worktree", str(wt))
        self.assertNotEqual(item["status"], "keep")

    def test_locked_worktree_and_missing_agent_metadata(self):
        wt = self.tmp / ".claude-worktrees" / "locked"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "locked")
        g(self.repo, "worktree", "lock", "--reason", "active agent", str(wt))
        data = self.snapshot()
        linked = next(r for r in data["repos"] if r["kind"] == "worktree")
        self.assertEqual(linked["readiness"]["resume"]["state"], "blocked")
        inventory = next(r for r in data["repos"] if r["kind"] == "repo")["worktree_inventory"][1]
        self.assertEqual(inventory["lock_reason"], "active agent")
        self.assertEqual(inventory["orphan_status"], "not_proven")
        self.assertEqual(inventory["cleanup_status"], "keep")
        g(self.repo, "worktree", "unlock", str(wt))
        # Only this fixture's directory is removed; simulate an externally lost checkout.
        import shutil
        shutil.rmtree(wt)
        data = self.snapshot()
        self.assertEqual(data["repos"][0]["worktree_inventory"][1]["orphan_status"], "missing")
        self.assertTrue(data["repos"][0]["worktree_inventory"][1]["stale"])

    def test_failed_fetch_invalidates_earlier_success(self):
        repo = rp.Repo(str(self.repo))
        self.assertIsNone(repo.fetch())
        self.assertTrue(repo.fresh())
        g(self.repo, "remote", "set-url", "origin", str(self.tmp / "offline.git"))
        self.assertIsNotNone(repo.fetch())
        self.assertFalse(repo.fresh())
        row = self.snapshot()["repos"][0]
        self.assertFalse(row["fresh"])
        self.assertEqual(row["remotes"][0]["contact"], "failed")
        self.assertEqual(row["readiness"]["sync"]["state"], "review")

    def test_multiple_remotes_do_not_share_fetch_freshness(self):
        self.fetch()
        g(self.repo, "remote", "add", "mirror", str(self.remote))
        repo = rp.Repo(str(self.repo))
        self.assertFalse(repo.fresh())
        self.assertIsNone(repo.fetch())
        row = self.snapshot()["repos"][0]
        remotes = {r["name"]: r for r in row["remotes"]}
        self.assertTrue(remotes["origin"]["fresh"])
        self.assertFalse(remotes["mirror"]["fresh"])
        self.assertEqual(remotes["mirror"]["contact"], "not_checked")

    def test_remote_credentials_are_redacted(self):
        g(self.repo, "remote", "add", "secret", "https://user:token@example.com/repo.git?access_token=secret#fragment")
        row = self.snapshot()["repos"][0]
        secret = next(r for r in row["remotes"] if r["name"] == "secret")
        self.assertEqual(secret["fetch_urls"], ["https://example.com/repo.git"])
        self.assertEqual(secret["push_urls"], ["https://example.com/repo.git"])

    def test_upstream_ahead_differs_from_commits_on_no_remote(self):
        commit(self.repo, "new.txt")
        g(self.repo, "push", "-q", "origin", "HEAD:elsewhere")
        row = self.snapshot()["repos"][0]
        self.assertEqual(row["branch_ahead"], 1)
        self.assertEqual(row["branches"][0]["upstream_ahead"], 1)
        self.assertEqual(row["branches"][0]["unpushed"], 0)
        self.assertEqual(row["unpushed_branches"], [])

    def test_unreadable_measurement_is_partial_and_not_ready_to_clean(self):
        commit(self.repo, ".gitignore", "node_modules/\n")
        commit(self.repo, "package.json", "{}")
        folder = self.repo / "node_modules"
        folder.mkdir()
        real_scandir = rp.os.scandir

        def unreadable(path):
            if rp._norm_key(str(path)) == rp._norm_key(str(folder)):
                raise PermissionError("fixture directory locked")
            return real_scandir(path)

        with patch.object(rp.os, "scandir", side_effect=unreadable):
            data = self.snapshot()
        self.assertFalse(data["complete"])
        row = data["repos"][0]
        self.assertEqual(row["risk"]["level"], "unknown")
        self.assertEqual(row["readiness"]["clean"]["safe_item_ids"], [])

    def test_incremental_sizes_never_cache_git_or_safety(self):
        commit(self.repo, ".gitignore", "node_modules/\n")
        commit(self.repo, "package.json", "{}")
        g(self.repo, "push", "-q")
        self.fetch()
        junk = self.repo / "node_modules"
        junk.mkdir()
        (junk / "large.dat").write_bytes(b"a" * 10000)
        initial = self.snapshot()
        previous = self.tmp / "previous.json"
        previous.write_text(json.dumps(initial), encoding="utf-8")
        commit(self.repo, "unpushed.txt")
        (self.repo / "README.md").write_text("dirty now")
        (junk / "large.dat").write_bytes(b"b" * 20000)
        # Changing tracking invalidates classification even when size is reused.
        g(self.repo, "add", "-f", "node_modules/large.dat")
        with patch.object(rp.os, "scandir", wraps=rp.os.scandir) as walks:
            data = self.snapshot("--previous", str(previous))
        self.assertFalse(any(rp._norm_key(str(c.args[0])) == rp._norm_key(str(junk)) for c in walks.call_args_list))
        row = data["repos"][0]
        self.assertGreater(data["refresh"]["reused_measurements"], 0)
        self.assertTrue(row["disk"]["estimated"])
        self.assertGreater(row["dirty"]["total"], 0)
        self.assertEqual(row["branch_ahead"], 1)
        self.assertEqual(by_target(row, "folder", "node_modules")["status"], "keep")
        fresh = self.snapshot("--previous", str(previous), "--max-size-age", "0")
        self.assertEqual(fresh["refresh"]["reused_measurements"], 0)
        self.assertGreater(fresh["repos"][0]["worktree_bytes"], row["worktree_bytes"])
        invalid = self.tmp / "bad.json"
        invalid.write_text('{"schema_version":"999"}')
        with self.assertRaises(ValueError):
            self.snapshot("--previous", str(invalid))

    def test_interrupted_operation_and_index_lock_protect_readiness(self):
        rp.log_operation(str(self.dest), {"op": "clean", "op_id": "interrupted-fixture", "repo": str(self.repo),
                                        "phase": "started", "kind": "branch", "target": "feature"})
        lock = self.repo / ".git" / "index.lock"
        lock.write_text("held")
        row = self.snapshot()["repos"][0]
        self.assertEqual(row["readiness"]["resume"]["state"], "blocked")
        self.assertEqual(row["readiness"]["sync"]["state"], "blocked")
        self.assertEqual(row["readiness"]["clean"]["safe_item_ids"], [])
        self.assertEqual(row["interrupted_operations"][0]["op_id"], "interrupted-fixture")
        self.assertTrue(lock.exists())
        outcome = rp.cmd_sync(rp.build_parser().parse_args(["sync", "--repo", str(self.repo)]))
        self.assertFalse(outcome["ok"])

    def test_clean_totals_do_not_double_count_review_worktrees(self):
        wt = self.tmp / "agent"
        g(self.repo, "worktree", "add", "-q", str(wt), "-b", "agent")
        (wt / ".gitignore").write_text("build/\n.env\n")
        g(wt, "add", ".gitignore")
        g(wt, "commit", "-qm", "ignore")
        g(wt, "push", "-qu", "origin", "agent")
        self.fetch()
        (wt / ".env").write_text("secret")
        (wt / "build").mkdir()
        (wt / "build" / "output").write_bytes(b"a" * 5000)
        data = self.snapshot()
        main = next(r for r in data["repos"] if r["kind"] == "repo")
        item = by_target(main, "worktree", str(wt))
        self.assertEqual(item["status"], "review")
        self.assertEqual(data["totals"]["review_bytes"], item["bytes"])
        self.assertEqual(data["totals"]["safe_bytes"], 0)

    def test_empty_and_bare_repositories(self):
        empty = self.tmp / "empty"
        g(self.tmp, "init", "-q", str(empty))
        data = rp.snapshot(repos=[str(empty), str(self.remote)], backup_dir=str(self.dest))
        self.assertTrue(data["complete"], data)
        self.assertTrue(next(r for r in data["repos"] if r["path"] == str(empty))["unborn"])
        bare = next(r for r in data["repos"] if r["bare"])
        self.assertEqual(bare["readiness"]["resume"]["state"], "review")
        nested = rp.snapshot(repos=[str(self.remote / "objects")], backup_dir=str(self.dest))
        self.assertFalse(nested["complete"])
        self.assertIn("error", nested["repos"][0])

    def test_python_api_and_aura_adapter(self):
        spec = importlib.util.spec_from_file_location("aura_client", Path(rp.__file__).parent / "examples" / "aura_client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        data = client.read_health(rp.__file__, [str(self.repo)])
        self.assertEqual(data["schema_version"], "1.0")
        with self.assertRaises(client.SnapshotError):
            client.read_health(rp.__file__, [str(self.tmp / "missing")])
        with patch.object(client.subprocess, "run", side_effect=subprocess.TimeoutExpired("snapshot", 1)):
            with self.assertRaises(client.SnapshotError):
                client.read_health(rp.__file__, [str(self.repo)])
        for change in ({"schema_version": 42}, {"scanned_at": time.time() - 1000}, {"complete": False}):
            invalid = {**data, **change}
            completed = subprocess.CompletedProcess([], 0, json.dumps(invalid).encode(), b"")
            with patch.object(client.subprocess, "run", return_value=completed):
                with self.assertRaises(client.SnapshotError):
                    client.read_health(rp.__file__, [str(self.repo)])
        self.assertTrue(rp.snapshot(repos=[str(self.repo)], backup_dir=str(self.dest))["read_only"])

    def test_sync_refuses_failed_comparison_and_status(self):
        other = self.other_clone()
        commit(other, "remote.txt")
        g(other, "push", "-q")
        real = rp.git_ok

        def fail_comparison(path, *args, **kwargs):
            if args[:3] == ("rev-list", "--left-right", "--count"):
                return None
            return real(path, *args, **kwargs)

        args = rp.build_parser().parse_args(["sync", "--repo", str(self.repo)])
        head = g(self.repo, "rev-parse", "HEAD")
        with patch.object(rp, "git_ok", side_effect=fail_comparison):
            self.assertFalse(rp.cmd_sync(args)["ok"])
        with patch.object(rp, "dirty_state", return_value={"counts": {"total": 0}, "files": [], "error": "locked read"}):
            self.assertFalse(rp.cmd_sync(args)["ok"])
        self.assertEqual(g(self.repo, "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
