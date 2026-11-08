"""The store side of the OpenSpec flow (proposal T04, contract C2): a Feature is a record of its
own in the store file (ADR-0001), its number comes from the same folder counter the Tasks use
(ADR-0002), so a ref is a Task or a Feature and never both, and old task code still reads a store
file that carries a `features` list.

A throwaway STORE (tempfile) is used; no real HOME, HERDR_APP_HOME or ~/.herdr is touched."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
sys.dont_write_bytecode = True
# STORE is read from this at import time; the per-test dir is set on the module below.
os.environ.setdefault("HERDR_TASK_STORE", tempfile.mkdtemp(prefix="herdr-store-import-"))

from herdr_task import store  # noqa: E402
from herdr_task.base import Fail  # noqa: E402

# The contract fixture of C10 (test/fixtures/openspec-flow/store.json, owned by task T00 and not on
# this branch yet). Kept here verbatim so this task's test is self-contained; the real file is also
# loaded when it is present (test_contract_fixture_file_on_disk_loads).
CONTRACT_FIXTURE = json.loads(r"""
{
  "version": 1,
  "folder": "/tmp/throwaway/shop",
  "kind": "repo",
  "name": "shop",
  "slug": "shop",
  "prefix": "HAT",
  "next": 20,
  "features": [
    {
      "id": 12, "title": "Login flow", "change": "add-login-flow",
      "branch": "feature/HAT-12-login-flow", "base": "main", "mode": "remote",
      "phase": "building", "attention": "", "spec_task": 13,
      "children": [
        { "ref": 14, "machine": null, "package": "1.1", "rows": ["1.1"], "role": "child" },
        { "ref": 15, "machine": "vm-b", "package": "1.2", "rows": ["1.2"], "role": "child" },
        { "ref": 16, "machine": null, "package": "2.1", "rows": ["2.1"], "role": "child" }
      ],
      "verify_task": 17, "fix_rounds": 0, "auto_fanout": true,
      "digests": {
        "local:14": {
          "ref": "HAT-14", "role": "child", "feature": 12, "rows": ["1.1"], "state": "merged",
          "machine": null, "review": { "blocker": 0, "major": 1, "minor": 2 }, "rounds": 1,
          "events": { "needs_you": 0, "stalled": 0, "failed": 0 }, "rebase_conflicts": 0,
          "time": { "estimate_min": 30, "actual_min": 42 },
          "top_tools": [ { "tool": "Bash", "calls": 12, "ms": 34000 } ],
          "transcript": "/tmp/throwaway/logs/HAT-14.jsonl"
        }
      },
      "created": "2026-11-07T10:00:00Z"
    },
    {
      "id": 18, "title": "Checkout polish", "change": "polish-checkout",
      "branch": "feature/HAT-18-checkout-polish", "base": "main", "mode": "local",
      "phase": "planning", "attention": "", "spec_task": 19, "children": [],
      "verify_task": null, "fix_rounds": 0, "auto_fanout": true, "digests": {},
      "created": "2026-11-07T11:30:00Z"
    }
  ],
  "tasks": [
    { "id": 13, "title": "Plan the login flow", "prompt": "Write the OpenSpec change for the login flow.",
      "mode": "worktree", "state": "done", "created": "2026-11-07T10:00:00Z", "agents": [],
      "feature": 12, "role": "spec", "branch": "task/HAT-12-HAT-13-plan" },
    { "id": 14, "title": "Shared parser types", "prompt": "Do work package 1.1.", "mode": "worktree",
      "state": "done", "created": "2026-11-07T10:05:00Z", "agents": [], "feature": 12, "role": "child",
      "owns": ["src/parser/types.ts"], "risk": "low", "rows": ["1.1"], "retries": 0, "skipped": false,
      "branch": "task/HAT-12-HAT-14-types" },
    { "id": 15, "title": "Parse body", "prompt": "Do work package 1.2.", "mode": "worktree",
      "state": "running", "created": "2026-11-07T10:05:00Z", "agents": [], "feature": 12, "role": "child",
      "owns": ["src/parser/body.ts"], "risk": "high", "rows": ["1.2"], "retries": 0, "skipped": false,
      "branch": "task/HAT-12-HAT-15-body" },
    { "id": 16, "title": "Wire parser", "prompt": "Do work package 2.1.", "mode": "worktree",
      "state": "queued", "created": "2026-11-07T10:05:00Z", "agents": [], "feature": 12, "role": "child",
      "owns": ["src/parser/index.ts"], "risk": "low", "rows": ["2.1"], "retries": 0, "skipped": false,
      "after": [14, 15], "branch": "task/HAT-12-HAT-16-wire" },
    { "id": 17, "title": "Verify login flow", "prompt": "Run /opsx:verify add-login-flow.",
      "mode": "worktree", "state": "queued", "created": "2026-11-07T10:05:00Z", "agents": [],
      "feature": 12, "role": "verify", "after": [14, 15, 16], "branch": "task/HAT-12-HAT-17-verify" },
    { "id": 19, "title": "Plan the checkout polish", "prompt": "Write the OpenSpec change for the checkout polish.",
      "mode": "worktree", "state": "running", "created": "2026-11-07T11:30:00Z", "agents": [],
      "feature": 18, "role": "spec", "branch": "task/HAT-18-HAT-19-plan" },
    { "id": 7, "title": "Fix a typo in the footer", "prompt": "Classic task, no feature.",
      "mode": "worktree", "state": "ready", "created": "2026-11-06T09:00:00Z", "agents": [] }
  ]
}
""")


class FeatureStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="herdr-store-")
        self._saved_store = store.STORE
        store.STORE = self.tmp
        self.loc = {"folder": "/tmp/throwaway/shop", "kind": "repo", "name": "shop", "common": None}

    def tearDown(self):
        store.STORE = self._saved_store
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data, folder=None):
        """Put a store file where the current code would look for it."""
        _, path = store.store_path(folder or self.loc["folder"])
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

    def _loc(self, folder):
        return {"folder": folder, "kind": "repo", "name": os.path.basename(folder), "common": None}

    # ---- the features list -------------------------------------------------------------------

    def test_a_fresh_store_has_an_empty_features_list(self):
        with store.Store(self.loc) as s:
            self.assertEqual(s.data["features"], [])

    def test_the_write_path_keeps_the_features_list(self):
        """Saving the store (the ordinary task write path) does not drop features."""
        self._write(CONTRACT_FIXTURE)
        with store.Store(self.loc) as s:
            s.new("A later classic task", "", "worktree")  # a write, no feature touched
        with store.Store(self.loc, write=False) as s:
            self.assertEqual([f["id"] for f in s.data["features"]], [12, 18])
            self.assertEqual(s.feature_by_id(12)["phase"], "building")

    # ---- old task code reads a file that has features ----------------------------------------

    def test_old_task_code_reads_a_file_that_has_features(self):
        """The task-oriented code path (find, the tasks list, the counter) works unchanged on a
        store file that carries a features list; it keys on `tasks` and ignores `features`."""
        self._write(CONTRACT_FIXTURE)
        with store.Store(self.loc, write=False) as s:
            # The classic task is found exactly as before.
            classic = s.find("HAT-7")
            self.assertEqual(classic["title"], "Fix a typo in the footer")
            self.assertNotIn("feature", classic)
            # The tasks list is only the tasks; the features are not mixed in.
            self.assertEqual(len(s.data["tasks"]), 7)
            self.assertTrue(all("phase" not in t for t in s.data["tasks"]))
            self.assertEqual(s.data["next"], 20)

    def test_a_store_with_features_is_still_a_task_list(self):
        """is_task_list (the one rule the panels share) accepts a file that has a features list."""
        path = self._write(CONTRACT_FIXTURE)
        self.assertTrue(store.is_task_list(CONTRACT_FIXTURE))
        # A file that carries features is listed as a task list by the folder scan.
        self.assertIn(path, store.store_files())
        self.assertTrue(store.is_task_list(store.read_list(path)))

    # ---- one shared counter: a ref is a Task or a Feature, never both ------------------------

    def test_a_feature_number_comes_from_the_same_counter(self):
        with store.Store(self.loc) as s:
            f1 = s.new_feature("Login", "add-login", "feature/x-login")
            t1 = s.new("A child", "", "worktree", feature=f1["id"], role="child")
            f2 = s.new_feature("Checkout", "polish-checkout", "feature/x-checkout")
            t2 = s.new("Classic", "", "worktree")
            self.assertEqual([f1["id"], t1["id"], f2["id"], t2["id"]], [1, 2, 3, 4])
            self.assertEqual(s.data["next"], 5)

    def test_a_ref_never_matches_both_a_feature_and_a_task(self):
        with store.Store(self.loc) as s:
            f = s.new_feature("Login", "add-login", "feature/x-login")
            t = s.new("A child", "", "worktree", feature=f["id"])
            ids = {x["id"] for x in s.data["tasks"]} & {x["id"] for x in s.data["features"]}
            self.assertEqual(ids, set())
            # find() is tasks only; a feature ref is a clear error, not a silent task match.
            self.assertIs(s.find(s.ref(t)), t)
            with self.assertRaises(Fail):
                s.find(s.ref(f))
            # find_feature() is features only; a task ref is a clear error.
            self.assertIs(s.find_feature(s.ref(f)), f)
            with self.assertRaises(Fail):
                s.find_feature(s.ref(t))
            # resolve() takes either and says which; it is never ambiguous.
            self.assertEqual(s.resolve(s.ref(f)), ("feature", f))
            self.assertEqual(s.resolve(s.ref(t)), ("task", t))

    def test_resolution_of_a_missing_ref_raises(self):
        with store.Store(self.loc) as s:
            s.new_feature("Login", "add-login", "feature/x-login")
            for call in (s.find, s.find_feature, s.resolve):
                with self.assertRaises(Fail):
                    call("HAT-999")

    # ---- the feature field (and the other flow fields) on a Task -----------------------------

    def test_new_stamps_the_feature_and_flow_fields_on_a_task(self):
        with store.Store(self.loc) as s:
            t = s.new("Child", "do 1.1", "worktree", feature=12, role="child",
                      owns=["src/a.ts"], risk="low", rows=["1.1"])
            self.assertEqual(t["feature"], 12)
            self.assertEqual(t["role"], "child")
            self.assertEqual(t["owns"], ["src/a.ts"])
            self.assertEqual(t["rows"], ["1.1"])

    def test_a_classic_task_carries_no_flow_fields(self):
        with store.Store(self.loc) as s:
            t = s.new("Classic", "", "here")
            for field in ("feature", "role", "owns", "risk", "rows"):
                self.assertNotIn(field, t)

    def test_new_feature_record_shape_matches_the_contract(self):
        with store.Store(self.loc) as s:
            f = s.new_feature("Login flow", "add-login-flow", "feature/x-login")
            self.assertEqual(set(f), set(CONTRACT_FIXTURE["features"][0]))
            self.assertEqual(
                (f["phase"], f["attention"], f["children"], f["verify_task"],
                 f["fix_rounds"], f["auto_fanout"], f["digests"]),
                ("planning", "", [], None, 0, True, {}))

    # ---- the contract fixture loads ----------------------------------------------------------

    def test_the_contract_fixture_store_loads(self):
        self._write(CONTRACT_FIXTURE)
        with store.Store(self.loc, write=False) as s:
            self.assertEqual([f["id"] for f in s.data["features"]], [12, 18])
            login = s.find_feature("HAT-12")
            self.assertEqual(login["phase"], "building")
            self.assertEqual(len(login["children"]), 3)
            self.assertEqual(login["digests"]["local:14"]["review"], {"blocker": 0, "major": 1, "minor": 2})
            spec = s.find("HAT-13")
            self.assertEqual((spec["feature"], spec["role"]), (12, "spec"))
            self.assertEqual(s.resolve("HAT-18"), ("feature", s.find_feature("HAT-18")))
            self.assertEqual(s.resolve("HAT-7")[0], "task")

    def test_contract_fixture_file_on_disk_loads(self):
        """When task T00's real fixture is present, it loads through the store too."""
        path = os.path.join(ROOT, "test", "fixtures", "openspec-flow", "store.json")
        if not os.path.exists(path):
            self.skipTest("the T00 fixture is not on this branch yet")
        with open(path) as f:
            disk = json.load(f)
        self._write(disk, folder=disk["folder"])
        with store.Store(self._loc(disk["folder"]), write=False) as s:
            self.assertEqual(s.data["features"], disk["features"])
            self.assertEqual(len(s.data["tasks"]), len(disk["tasks"]))


if __name__ == "__main__":
    unittest.main()
