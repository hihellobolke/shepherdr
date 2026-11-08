"""retro_data.py: the retro JSON (contract C5). A Feature's digests plus the state of the repo
check script and CI. Mocks only gh; the repo and Store are real throwaway files (AGENTS.md). A
Feature with no children is still a valid result."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.dont_write_bytecode = True
if BIN not in sys.path:
    sys.path.insert(0, BIN)


class RetroBase(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        # realpath: macOS $TMPDIR (/var/folders/...) is a symlink to /private/var/...; store.locate
        # resolves the repo folder, so the temp root is resolved too to keep slugs consistent.
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        from herdr_task import store, retro_data, base
        self.store, self.retro_data, self.base = store, retro_data, base
        # store.STORE is bound from the env at import; setting HERDR_TASK_STORE here would be too
        # late and the throwaway store would leak into the real ~/.herdr/tasks (polluting it and
        # climbing the prefix dedup, e.g. REPO -> REPO35). Patch the module global instead.
        p = mock.patch.object(store, "STORE", os.path.join(self.tmp, "store"))
        p.start()
        self.addCleanup(p.stop)
        # Mock gh only (AGENTS.md): a successful last run by default.
        self._orig_gh = retro_data.gh
        self.gh_result = (True, json.dumps([{"conclusion": "success", "url": "https://x/run/1"}]), "")
        retro_data.gh = lambda *a, **k: self.gh_result

    def tearDown(self):
        self.retro_data.gh = self._orig_gh
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check_script(self, name="bin/check", exit_code=0):
        p = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\nexit {exit_code}\n")
        os.chmod(p, 0o755)

    def _workflow(self, name="check.yml"):
        d = os.path.join(self.repo, ".github", "workflows")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("name: Check\n")

    def _store(self):
        return self.store.Store(self.store.locate(self.repo))


class CheckState(RetroBase):
    def test_none_when_no_script(self):
        self.assertIsNone(self.retro_data.check_state(self.repo))

    def test_passing_script(self):
        self._check_script(exit_code=0)
        st = self.retro_data.check_state(self.repo)
        self.assertEqual(st["script"], "bin/check")
        self.assertTrue(st["last"]["ok"])
        self.assertTrue(st["last"]["ran_at"].endswith("Z"))

    def test_failing_script(self):
        self._check_script(exit_code=1)
        st = self.retro_data.check_state(self.repo)
        self.assertFalse(st["last"]["ok"])

    def test_sync_shared_is_preferred(self):
        self._check_script(name="bin/sync-shared", exit_code=0)
        self.assertEqual(self.retro_data.check_state(self.repo)["script"], "bin/sync-shared --check")


class CiState(RetroBase):
    def test_none_when_no_workflows(self):
        self.assertIsNone(self.retro_data.ci_state(self.repo))

    def test_workflow_and_last_run(self):
        self._workflow()
        st = self.retro_data.ci_state(self.repo)
        self.assertEqual(st["workflow"], os.path.join(".github", "workflows", "check.yml"))
        self.assertEqual(st["last"], {"conclusion": "success", "url": "https://x/run/1"})

    def test_prefers_check_yml(self):
        self._workflow(name="other.yml")
        self._workflow(name="check.yml")
        self.assertTrue(self.retro_data.ci_state(self.repo)["workflow"].endswith("check.yml"))

    def test_last_none_when_gh_unavailable(self):
        self._workflow()
        self.gh_result = (False, "", "gh isn't installed here")
        st = self.retro_data.ci_state(self.repo)
        self.assertEqual(st["workflow"], os.path.join(".github", "workflows", "check.yml"))
        self.assertIsNone(st["last"])


class Build(RetroBase):
    def test_feature_with_children(self):
        self._check_script(exit_code=0)
        self._workflow()
        with self._store() as s:
            feat = {"id": 12, "title": "Login flow", "change": "add-login-flow",
                    "digests": {"local:14": {"ref": "X-14", "rows": ["2.1"]},
                                "vm-b:15": {"ref": "X-15", "rows": ["2.2"]}}}
            data = self.retro_data.build(s, feat)
        self.assertEqual(data["feature"], {"ref": "REPO-12", "title": "Login flow", "change": "add-login-flow"})
        self.assertEqual([d["ref"] for d in data["digests"]], ["X-14", "X-15"])
        self.assertEqual(data["checks"]["script"], "bin/check")
        self.assertTrue(data["ci"]["workflow"].endswith("check.yml"))

    def test_feature_with_no_children_is_valid(self):
        with self._store() as s:
            feat = {"id": 13, "title": "Empty", "change": "c2", "digests": {}}
            data = self.retro_data.build(s, feat)
        self.assertEqual(data["digests"], [])
        self.assertIsNone(data["checks"])  # no check script in the repo
        self.assertIsNone(data["ci"])      # no workflows in the repo
        self.assertEqual(set(data.keys()), {"feature", "digests", "checks", "ci"})


class FindFeature(RetroBase):
    def test_found(self):
        with self._store() as s:
            s.data["features"] = [{"id": 12, "title": "F"}]
            got = self.retro_data._find_feature(s, f"{s.prefix}-12")
            self.assertEqual(got["id"], 12)

    def test_a_task_is_not_a_feature(self):
        with self._store() as s:
            t = s.new("A task")
            with self.assertRaises(self.base.Fail):
                self.retro_data._find_feature(s, f"{s.prefix}-{t['id']}")

    def test_missing(self):
        with self._store() as s:
            with self.assertRaises(self.base.Fail):
                self.retro_data._find_feature(s, f"{s.prefix}-99")

    def test_wrong_prefix(self):
        with self._store() as s:
            s.data["features"] = [{"id": 1, "title": "F"}]
            with self.assertRaises(self.base.Fail):
                self.retro_data._find_feature(s, "ZZZ-1")


class Command(RetroBase):
    def test_cmd_prints_the_retro_json(self):
        with self._store() as s:
            s.data["features"] = [{"id": 12, "title": "Login", "change": "add-login", "digests": {}}]
            s.dirty = True
            ref = f"{s.prefix}-12"
        args = types.SimpleNamespace(id=ref, folder=self.repo, json=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.retro_data.cmd_retro_data(args)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["feature"]["ref"], ref)
        self.assertEqual(data["digests"], [])


if __name__ == "__main__":
    unittest.main()
