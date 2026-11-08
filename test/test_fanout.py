"""Fan-out (proposal T05b, contract C5/C6/C7): turn an approved work table into the child Tasks of a
Feature. The tests run against a real throwaway git repo and a throwaway STORE (tempfile); only `hc`
and `gh` would be mocked, and the home-machine paths here need neither, so nothing is mocked. The
repo rule holds: a test never runs a changed herdr-task on another machine, so the placement across
machines is checked through the dry-run plan, not by creating remote children.

They cover the dry run, repeat (idempotent) runs, the deterministic order, placement with chain
affinity, the local-mode refusal of other machines, amendments, every work-table error code, risk
(the risk column and risky paths), the generated after links and Verify Task, and the `openspec
validate` plan guard."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
sys.dont_write_bytecode = True
os.environ.setdefault("HERDR_TASK_STORE", tempfile.mkdtemp(prefix="herdr-fanout-store-"))
os.environ.setdefault("HERDR_TASK_SETTINGS", os.path.join(tempfile.mkdtemp(prefix="herdr-fanout-cfg-"), "shepherdr.json"))

from herdr_task import fanout, store as st  # noqa: E402
from herdr_task.base import Fail  # noqa: E402
from herdr_task import settings as settings_mod  # noqa: E402


VALID_TABLE = """# Login flow

## Work table

| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 0.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 1.1 | Parse headers       | src/parser/header.ts | 0.1     | low  | S    |
| 1.2 | Parse body          | src/parser/body.ts   | 0.1     | high | M    |
| 2.1 | Wire parser         | src/parser/index.ts  | 1.1,1.2 | low  | M    |

## Tasks

- [ ] 0.1 Shared parser types
- [ ] 1.1 Parse headers
- [ ] 1.2 Parse body
- [ ] 2.1 Wire parser
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


class Base(unittest.TestCase):
    """A fresh throwaway git repo with an OpenSpec change and a Feature in its store. The approved work
    table lives on the feature branch (contract C8), so `write_table` commits it there and leaves the
    main checkout clean; a test that stands in for the spec worktree writes to a working tree instead."""

    change = "add-login-flow"

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="herdr-fanout-repo-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "commit", "--allow-empty", "-m", "init")
        self.main_branch = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "--abbrev-ref", "HEAD"],
            stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        self.feature_branch = None
        self.loc = st.locate(self.repo)

    def make_feature(self, mode="remote", change=None, auto_fanout=True):
        change = change or self.change
        with st.Store(self.loc) as s:
            f = s.new_feature("Login flow", change, "feature/tmp", mode=mode, auto_fanout=auto_fanout)
            spec = s.new("Plan the change", "", mode="worktree", feature=f["id"], role="spec")
            f["spec_task"] = spec["id"]
            f["branch"] = f"feature/{s.ref(f)}-login-flow"
            f["phase"] = "building"
            ref, branch = s.ref(f), f["branch"]
        _git(self.repo, "branch", branch, self.main_branch)  # the feature branch, not checked out
        self.feature_branch = branch
        return ref

    def write_table(self, text, change=None, branch=None):
        """Commit tasks.md on the feature branch (where the approved table lives), keeping main clean."""
        change = change or self.change
        branch = branch or self.feature_branch
        rel = f"openspec/changes/{change}/tasks.md"
        _git(self.repo, "checkout", "-q", branch)
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        _git(self.repo, "add", rel)
        _git(self.repo, "commit", "-q", "-m", f"table {change}")
        _git(self.repo, "checkout", "-q", self.main_branch)

    def write_worktree_table(self, folder, text, change=None):
        """Write tasks.md into a working tree without committing (the spec worktree, for check_plan)."""
        change = change or self.change
        cdir = os.path.join(folder, "openspec", "changes", change)
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "tasks.md"), "w") as f:
            f.write(text)

    def plan(self, **kw):
        with st.Store(self.loc, write=False) as s:
            return fanout.plan(s, self.change, **kw)

    def run_fanout(self, **kw):
        with st.Store(self.loc) as s:
            return fanout.run(s, self.change, **kw)

    def read(self):
        with st.Store(self.loc, write=False) as s:
            return json.loads(json.dumps(s.data))


# ---- the dry-run plan (C5) -------------------------------------------------------------------

class PlanTest(Base):
    def setUp(self):
        super().setUp()
        self.feature_ref = self.make_feature()
        self.write_table(VALID_TABLE)

    def test_valid_shape(self):
        p = self.plan()
        self.assertTrue(p["valid"])
        self.assertEqual(p["change"], self.change)
        self.assertEqual(p["feature"], self.feature_ref)
        self.assertEqual(p["waves"], [["0.1"], ["1.1", "1.2"], ["2.1"]])
        self.assertEqual(len(p["packages"]), 4)  # one package per row (max parallelism)
        for pkg in p["packages"]:
            self.assertEqual(len(pkg["rows"]), 1)
            self.assertTrue(pkg["branch"].startswith(f"task/{self.feature_ref}-"))
            self.assertEqual(pkg["base"], f"feature/{self.feature_ref}-login-flow")
            self.assertIsNone(pkg["machine"])
            self.assertIn("work package", pkg["brief"])

    def test_dry_run_creates_nothing(self):
        before = self.read()
        self.plan()
        self.assertEqual(self.read(), before)

    def test_order_wave_then_size(self):
        # Wave first; within a wave the longer chain, then larger size, then table order. 1.1 and 1.2
        # share a chain length, so the larger (M) 1.2 comes before the (S) 1.1.
        p = self.plan()
        self.assertEqual([pkg["package"] for pkg in p["packages"]], ["0.1", "1.2", "1.1", "2.1"])

    def test_risk_and_merge(self):
        p = {pkg["package"]: pkg for pkg in self.plan()["packages"]}
        self.assertEqual(p["1.2"]["risk"], "high")
        self.assertEqual(p["1.2"]["risk_reason"], "risk column is high")
        self.assertEqual(p["1.2"]["merge"], "ask")
        self.assertEqual(p["0.1"]["risk"], "low")
        self.assertEqual(p["0.1"]["merge"], "checks")

    def test_after_links_and_verify(self):
        p = self.plan()
        by = {pkg["package"]: pkg for pkg in p["packages"]}
        self.assertEqual(by["0.1"]["after"], [])  # a root comes after nothing (spec is already merged)
        self.assertEqual(by["1.1"]["depends"], ["0.1"])
        # a non-root comes after the one child of each package it depends on
        self.assertEqual(len(by["1.1"]["after"]), 1)
        self.assertTrue(by["1.1"]["after"][0].startswith(self.feature_ref.split("-")[0]))
        self.assertEqual(len(p["verify_task"]["after"]), 4)

    def test_determinism(self):
        self.assertEqual(self.plan(), self.plan())

    def test_no_feature_fails(self):
        with st.Store(self.loc, write=False) as s:
            with self.assertRaises(Fail):
                fanout.plan(s, "no-such-change")

    def test_reads_feature_branch_not_the_checkout(self):
        # Fan-out reads the approved table from the feature branch (where setUp committed VALID_TABLE),
        # even though the checkout is on main and main has no such table. `folder=` overrides that with a
        # working tree (the spec worktree for the ready plan guard).
        broken = ("## Work table\n\n| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
                  "| 0.1 | A | a.ts | 0.2 | low | S |\n| 0.2 | B | b.ts | 0.1 | low | S |\n")
        other = tempfile.mkdtemp(prefix="herdr-fanout-wt-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        self.write_worktree_table(other, broken)
        with st.Store(self.loc, write=False) as s:
            self.assertTrue(fanout.plan(s, self.change)["valid"])  # the feature branch's good table
            self.assertFalse(fanout.plan(s, self.change, folder=other)["valid"])  # the override tree


# ---- work-table error codes (C5/C6) ----------------------------------------------------------

class InvalidTableTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()

    def _plan_code(self, body):
        head = "## Work table\n\n| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
        self.write_table(head + body)
        return self.plan()

    def test_unknown_dep(self):
        p = self._plan_code("| 0.1 | A | a.ts | 9.9 | low | S |\n")
        self.assertFalse(p["valid"])
        self.assertEqual(p["code"], "unknown-dep")

    def test_cycle(self):
        p = self._plan_code("| 0.1 | A | a.ts | 0.2 | low | S |\n| 0.2 | B | b.ts | 0.1 | low | S |\n")
        self.assertEqual(p["code"], "cycle")

    def test_empty_owns(self):
        p = self._plan_code("| 0.1 | A |  |  | low | S |\n")
        self.assertEqual(p["code"], "empty-owns")

    def test_size_l(self):
        p = self._plan_code("| 0.1 | A | a.ts |  | low | L |\n")
        self.assertEqual(p["code"], "size-l")

    def test_bad_risk(self):
        p = self._plan_code("| 0.1 | A | a.ts |  | maybe | S |\n")
        self.assertEqual(p["code"], "bad-risk")

    def test_missing_tasks_md(self):
        p = self.plan()  # no tasks.md written
        self.assertFalse(p["valid"])
        self.assertEqual(p["code"], "bad-table")


# ---- the real run ----------------------------------------------------------------------------

class RunTest(Base):
    def setUp(self):
        super().setUp()
        self.feature_ref = self.make_feature()
        self.write_table(VALID_TABLE)

    def test_creates_children_and_verify(self):
        r = self.run_fanout()
        self.assertEqual(r["created"], {"children": 4, "verify": 1})
        self.assertEqual(r["started"], 5)
        data = self.read()
        feat = data["features"][0]
        self.assertEqual(len(feat["children"]), 4)
        self.assertTrue(feat["verify_task"])
        children = [t for t in data["tasks"] if t.get("role") == "child"]
        self.assertEqual(len(children), 4)
        for t in children:
            self.assertEqual(t["feature"], feat["id"])
            self.assertEqual(t["state"], "queued")
            self.assertEqual(t["mode"], "worktree")
            self.assertEqual(t["base"], feat["branch"])
            self.assertTrue(t["owns"])
            self.assertEqual(t["retries"], 0)
            self.assertFalse(t["skipped"])

    def test_merge_mode_by_risk(self):
        self.run_fanout()
        by_rows = {tuple(t["rows"]): t for t in self.read()["tasks"] if t.get("role") == "child"}
        self.assertEqual(by_rows[("1.2",)]["set"]["merge"], "ask")   # high risk
        self.assertEqual(by_rows[("0.1",)]["set"]["merge"], "checks")

    def test_after_links_between_waves(self):
        self.run_fanout()
        tasks = {t["id"]: t for t in self.read()["tasks"]}
        by_rows = {tuple(t["rows"]): t for t in tasks.values() if t.get("role") == "child"}
        shared = by_rows[("0.1",)]
        headers = by_rows[("1.1",)]
        wire = by_rows[("2.1",)]
        self.assertEqual(headers["after"], [shared["id"]])
        self.assertCountEqual(wire["after"], [by_rows[("1.1",)]["id"], by_rows[("1.2",)]["id"]])
        verify = next(t for t in tasks.values() if t.get("role") == "verify")
        self.assertCountEqual(verify["after"], [t["id"] for t in by_rows.values()])
        self.assertEqual(verify["owns"], [])

    def test_children_inherit_the_spec_task_space(self):
        # Give the spec Task a space; the children and the Verify Task must queue in that space, not the
        # default one.
        with st.Store(self.loc) as s:
            spec = s.task_by_id(s.data["features"][0]["spec_task"])
            spec["space"] = "vm-demo/sp1"
            s.dirty = True
        self.run_fanout()
        for t in self.read()["tasks"]:
            if t.get("role") in ("child", "verify"):
                self.assertEqual((t.get("queue") or {}).get("space"), "vm-demo/sp1")

    def test_no_start_leaves_todo(self):
        self.run_fanout(start=False)
        states = {t.get("role"): t["state"] for t in self.read()["tasks"] if t.get("role") in ("child", "verify")}
        self.assertEqual(states["child"], "todo")
        self.assertEqual(states["verify"], "todo")

    def test_rerun_is_idempotent(self):
        self.run_fanout()
        after_first = self.read()
        r2 = self.run_fanout()
        self.assertEqual(r2["created"], {"children": 0, "verify": 0})
        self.assertEqual(self.read()["tasks"], after_first["tasks"])
        self.assertEqual(len(self.read()["features"][0]["children"]), 4)

    def test_rerun_fills_a_gap(self):
        # Create everything, then drop one child from the store as if it was never made; a rerun adds
        # back only that one.
        self.run_fanout()
        with st.Store(self.loc) as s:
            feat = s.data["features"][0]
            gone = next(c for c in feat["children"] if c["rows"] == ["1.1"])
            feat["children"] = [c for c in feat["children"] if c is not gone]
            s.data["tasks"] = [t for t in s.data["tasks"] if t["id"] != gone["ref"]]
            s.dirty = True
        r = self.run_fanout()
        self.assertEqual(r["created"]["children"], 1)
        self.assertEqual(len(self.read()["features"][0]["children"]), 4)

    def test_invalid_table_raises(self):
        self.write_table("## Work table\n\nno table here\n")
        with st.Store(self.loc) as s:
            with self.assertRaises(Fail):
                fanout.run(s, self.change)

    def test_home_child_ids_excludes_other_machines(self):
        children = [
            {"ref": 50, "machine": None, "rows": ["0.1"]},
            {"ref": 51, "machine": "vm-b", "rows": ["1.1"]},
            {"ref": 52, "machine": None, "rows": ["1.2"]},
        ]
        self.assertEqual(fanout._home_child_ids(children), [50, 52])

    def test_verify_links_home_only_and_leaves_ext_to_the_integration(self):
        # Fan-out never queries another machine under the store lock, so it links the Verify Task to the
        # home children only and leaves any cross-machine after_ext (added by the integration) untouched.
        # A rerun with such an after_ext present makes no change and no cross-machine call.
        self.run_fanout()
        with st.Store(self.loc) as s:
            feat = s.data["features"][0]
            leaf = next(c for c in feat["children"] if c["rows"] == ["2.1"])
            leaf["machine"] = "vm-b"  # as if the integration placed this leaf elsewhere
            verify = s.task_by_id(feat["verify_task"])
            verify["after"] = [c["ref"] for c in feat["children"] if not c.get("machine")]
            verify["after_ext"] = [{"ref": f"vm-b:{s.prefix}-{leaf['ref']}", "machine": "vm-b",
                                    "prefix": s.prefix, "id": leaf["ref"], "state": "running", "checked": 1}]
            s.dirty = True
        with st.Store(self.loc) as s:
            feat = s.data["features"][0]
            before = json.loads(json.dumps(s.task_by_id(feat["verify_task"])))
            made = fanout._ensure_verify(s, feat, self.change, None, True, 0)  # no vm-b call, no rewrite
        self.assertFalse(made)
        after = self.read()["tasks"]
        verify = next(t for t in after if t.get("role") == "verify")
        self.assertEqual(verify, before)                       # unchanged
        self.assertTrue(verify.get("after_ext"))               # the integration's link is preserved


# ---- placement with chain affinity (section 6.13), via the dry run ----------------------------

class PlacementTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        # One wave-0 contract and two independent chains off it, to show the spread.
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Types | pkg/types.ts |  | low | S |\n"
            "| 1.1 | A | a/a.ts | 0.1 | low | S |\n"
            "| 1.2 | B | b/b.ts | 0.1 | low | M |\n"
            "| 2.1 | Wire | w/w.ts | 1.1,1.2 | low | M |\n"
        )

    def test_home_only_when_no_machines(self):
        for pkg in self.plan()["packages"]:
            self.assertIsNone(pkg["machine"])

    def test_chain_affinity(self):
        p = self.plan(machines=["vm-a", "vm-b"])
        where = {pkg["package"]: pkg["machine"] for pkg in p["packages"]}
        # Each package sits on a named machine, and a dependent chain stays on one machine.
        self.assertEqual(set(where.values()), {"vm-a", "vm-b"})
        self.assertEqual(where["0.1"], where["2.1"])  # the long chain 0.1 -> ... -> 2.1 stays together
        # The two wave-1 branches land on different machines (independent chains spread).
        self.assertNotEqual(where["1.1"], where["1.2"])

    def test_cross_machine_after_is_qualified(self):
        p = self.plan(machines=["vm-a", "vm-b"])
        by = {pkg["package"]: pkg for pkg in p["packages"]}
        # 2.1 depends on 1.1 and 1.2 which sit on different machines: one after link is machine:ref.
        self.assertTrue(any(":" in a for a in by["2.1"]["after"]))

    def test_deterministic_placement(self):
        self.assertEqual(self.plan(machines=["vm-a", "vm-b"]), self.plan(machines=["vm-a", "vm-b"]))

    def test_refusal_is_only_for_packages_that_need_creation(self):
        # The refusal fires for a package that still has to be made on another machine, but not for a
        # child already placed there (recorded by the integration layer): a plain home rerun then does
        # not refuse. (A full run with a remote child reaches that machine to link it, which the tests
        # never do, so this checks the refusal's own logic and the filter run() applies.)
        self.run_fanout()
        existing = None
        with st.Store(self.loc, write=False) as s:
            p = fanout.plan(s, self.change, machines=["vm-a", "vm-b"])
            existing = {frozenset(c["rows"]) for c in s.data["features"][0]["children"]}
        # Every package already has a child, so nothing needs creating: the filtered list is empty.
        needs_creation = [pkg for pkg in p["packages"] if frozenset(pkg["rows"]) not in existing]
        self.assertEqual(needs_creation, [])
        fanout._refuse_remote_creation(needs_creation)  # does not raise
        with self.assertRaises(Fail):
            fanout._refuse_remote_creation([{"machine": "vm-a", "rows": ["9.9"]}])

    def test_real_run_refuses_remote_placement(self):
        # The dry run previews the spread, but a real run creates on the home machine only: creating a
        # child on another machine needs contract support this module does not have, so it is refused
        # before anything is created, rather than shipping a broken or deadlocking child.
        before = self.read()
        with st.Store(self.loc) as s:
            with self.assertRaises(Fail) as cm:
                fanout.run(s, self.change, machines=["vm-a", "vm-b"])
        self.assertIn("home machine only", str(cm.exception))
        self.assertEqual(self.read()["tasks"], before["tasks"])  # nothing created


# ---- inferred dependency from overlapping owns (section 6.7 step 3) ---------------------------

class OverlapTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        # 1.1 and 0.1 touch the same place (0.1 owns a file under src/api, 1.1 a glob over it) with no
        # declared dependency, so fan-out must add one: the later row waits for the earlier.
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Base | src/api/base.ts |  | low | S |\n"
            "| 1.1 | All | src/api/*.ts |  | low | S |\n"
        )

    def test_overlap_adds_a_dependency_in_the_plan(self):
        by = {pkg["package"]: pkg for pkg in self.plan()["packages"]}
        self.assertIn("0.1", by["1.1"]["depends"])  # inferred, though the table declared none
        self.assertEqual(by["0.1"]["depends"], [])

    def test_overlap_dependency_becomes_an_after_link(self):
        self.run_fanout()
        tasks = {tuple(t["rows"]): t for t in self.read()["tasks"] if t.get("role") == "child"}
        self.assertEqual(tasks[("1.1",)]["after"], [tasks[("0.1",)]["id"]])


# ---- local mode refuses other machines (section 6.10) ----------------------------------------

class LocalModeTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature(mode="local")
        self.write_table(VALID_TABLE)

    def test_refuses_machines(self):
        with st.Store(self.loc, write=False) as s:
            with self.assertRaises(Fail) as cm:
                fanout.plan(s, self.change, machines=["vm-a"])
        self.assertIn("local mode", str(cm.exception))

    def test_home_only_is_fine(self):
        with st.Store(self.loc) as s:
            r = fanout.run(s, self.change)
        self.assertEqual(r["created"]["children"], 4)
        for pkg in r["packages"]:
            self.assertIsNone(pkg["machine"])


# ---- amendments (section 6.7) ----------------------------------------------------------------

class AmendTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | low | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
        )
        self.run_fanout()

    def _amend_table(self, rows):
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n" + rows)

    def test_adds_only_new_rows(self):
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | low | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
            "| 1.3 | N | src/n.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        self.assertEqual(r["created"]["children"], 1)
        rows = {tuple(c["rows"]) for c in self.read()["features"][0]["children"]}
        self.assertIn(("1.3",), rows)

    def test_cancels_unstarted_removed_child(self):
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        self.assertTrue(r.get("cancelled"))
        data = self.read()
        rows = {tuple(c["rows"]) for c in data["features"][0]["children"]}
        self.assertNotIn(("1.2",), rows)
        cancelled = [t for t in data["tasks"] if t.get("rows") == ["1.2"]][0]
        self.assertEqual(cancelled["state"], "cancelled")

    def test_started_removed_child_is_kept_with_warning(self):
        # Mark 1.2's child as running, then remove its row: it stays, and a warning names it.
        with st.Store(self.loc) as s:
            t = next(t for t in s.data["tasks"] if t.get("rows") == ["1.2"])
            t["state"] = "running"
            s.dirty = True
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        self.assertTrue(any("1.2" in w for w in r["warnings"]))
        rows = {tuple(c["rows"]) for c in self.read()["features"][0]["children"]}
        self.assertIn(("1.2",), rows)  # kept

    def test_removed_dependency_clears_after(self):
        # 1.1 depended on 0.1. An amendment drops 0.1 (unstarted) and drops 1.1's dependency on it.
        # 0.1's child is cancelled, and 1.1's after must no longer point at the cancelled Task.
        before = {tuple(t["rows"]): t["after"] for t in self.read()["tasks"]
                  if t.get("rows") == ["1.1"]}
        self.assertTrue(before[("1.1",)])  # it had an after link
        self._amend_table(
            "| 1.1 | H | src/h.ts |  | low | S |\n"
            "| 1.2 | B | src/b.ts |  | low | S |\n"
        )
        self.run_fanout(amend=True)
        after = {tuple(t["rows"]): t.get("after") or [] for t in self.read()["tasks"]
                 if t.get("role") == "child"}
        self.assertEqual(after[("1.1",)], [])  # the removed dependency was reconciled away

    def test_reversed_dependency_does_not_false_cycle(self):
        # Initially 1.1 depends on 0.1. An amendment reverses it: 0.1 now depends on 1.1, and 1.1 on
        # nothing. Reconciling one child at a time against the old links would look like a cycle; the
        # clear-then-set reconciliation must apply the (acyclic) new graph without raising.
        self._amend_table(
            "| 0.1 | Types | src/t.ts | 1.1 | low | S |\n"
            "| 1.1 | H | src/h.ts |  | low | S |\n"
            "| 1.2 | B | src/b.ts |  | low | S |\n"
        )
        self.run_fanout(amend=True)  # must not raise a false cycle
        tasks = self.read()["tasks"]
        after = {tuple(t["rows"]): t.get("after") or [] for t in tasks if t.get("role") == "child"}
        h_child = next(t for t in tasks if t.get("rows") == ["1.1"])
        self.assertEqual(after[("0.1",)], [h_child["id"]])  # 0.1 now waits for 1.1
        self.assertEqual(after[("1.1",)], [])               # 1.1 waits for nothing

    def test_risk_escalation_reaches_an_unstarted_child(self):
        # An amendment that raises a row to high risk must make its unstarted child merge=ask (C2).
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | high | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
        )
        self.run_fanout(amend=True)
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertEqual(child["risk"], "high")
        self.assertEqual(child["set"]["merge"], "ask")

    def test_risk_escalation_reaches_a_started_child(self):
        # Raising a running child to high risk must set merge=ask too: merge only gates the merge, so
        # this keeps a now-risky change from merging without the person, without disturbing its work.
        with st.Store(self.loc) as s:
            t = next(t for t in s.data["tasks"] if t.get("rows") == ["1.1"])
            t["state"], t["started"] = "running", 1700000000000
            s.dirty = True
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/h.ts | 0.1 | high | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertEqual(child["risk"], "high")
        self.assertEqual(child["set"]["merge"], "ask")
        self.assertEqual(child["owns"], ["src/h.ts"])  # owns untouched on the running child
        self.assertTrue(any("1.1" in w for w in r["warnings"]))

    def test_started_child_is_not_cancelled_even_if_requeued(self):
        # A child that ran and was re-queued (state queued, but with a start history) must not be
        # cancelled by an amendment that drops its row; it is kept with a warning.
        with st.Store(self.loc) as s:
            t = next(t for t in s.data["tasks"] if t.get("rows") == ["1.1"])
            t["state"], t["started"], t["worktree"] = "queued", 1700000000000, "/tmp/wt"
            s.dirty = True
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        rows = {tuple(c["rows"]) for c in self.read()["features"][0]["children"]}
        self.assertIn(("1.1",), rows)  # kept, not cancelled
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertNotEqual(child["state"], "cancelled")
        self.assertTrue(any("1.1" in w for w in r["warnings"]))

    def test_changed_owns_of_existing_child_warns(self):
        # Keep row 1.1 but change its owned paths. The running child keeps its owns; a warning names it.
        self._amend_table(
            "| 0.1 | Types | src/t.ts |  | low | S |\n"
            "| 1.1 | H | src/moved.ts | 0.1 | low | S |\n"
            "| 1.2 | B | src/b.ts | 0.1 | low | S |\n"
        )
        r = self.run_fanout(amend=True)
        self.assertTrue(any("owns" in w and "1.1" in w for w in r["warnings"]))
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertEqual(child["owns"], ["src/h.ts"])  # unchanged under the running child


# ---- an amendment must not remove a still-risky child's approval gate -------------------------

class RiskDowngradeTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        with open(settings_mod.SETTINGS, "w") as f:
            json.dump({"everywhere": {"tasks": {"risky_paths": ["security/**"]}}}, f)
        self.addCleanup(lambda: os.path.exists(settings_mod.SETTINGS) and os.remove(settings_mod.SETTINGS))
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Base | src/base.ts |  | low | S |\n"
            "| 1.1 | Auth | security/auth.py | 0.1 | low | S |\n"
        )
        self.run_fanout()

    def test_amendment_keeps_the_gate_when_owns_stay_risky(self):
        # 1.1 is high because it owns a risky path; its child is running. An amendment changes the row's
        # owns to a low-risk path, but the running child keeps its risky owned paths, so its approval
        # gate (merge=ask) must stay: an amendment cannot quietly let a risky change auto-merge.
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertEqual(child["set"]["merge"], "ask")  # high at creation
        with st.Store(self.loc) as s:
            t = next(t for t in s.data["tasks"] if t.get("rows") == ["1.1"])
            t["state"], t["started"] = "running", 1700000000000
            s.dirty = True
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Base | src/base.ts |  | low | S |\n"
            "| 1.1 | Auth | ui/login.ts | 0.1 | low | S |\n")
        self.run_fanout(amend=True)
        child = next(t for t in self.read()["tasks"] if t.get("rows") == ["1.1"])
        self.assertEqual(child["risk"], "high")             # still risky by its retained owns
        self.assertEqual(child["set"]["merge"], "ask")      # the gate stays
        self.assertEqual(child["owns"], ["security/auth.py"])  # owns untouched on the running child


# ---- risk from risky paths (section 6.6) -----------------------------------------------------

class RiskyPathTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        self.write_table(
            "## Work table\n\n"
            "| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | Config | config/app.json |  | low | S |\n"
        )

    def test_owns_matching_risky_path_is_high(self):
        with open(settings_mod.SETTINGS, "w") as f:
            json.dump({"everywhere": {"tasks": {"risky_paths": ["config/*.json"]}}}, f)
        self.addCleanup(lambda: os.path.exists(settings_mod.SETTINGS) and os.remove(settings_mod.SETTINGS))
        pkg = self.plan()["packages"][0]
        self.assertEqual(pkg["risk"], "high")
        self.assertIn("risky path", pkg["risk_reason"])
        self.assertEqual(pkg["merge"], "ask")

    def test_risky_paths_are_read_for_the_feature_space(self):
        # The risky path is set in the space the children run in (the spec Task's). Risk classification
        # must read that space's setting, not another space's.
        with st.Store(self.loc) as s:
            s.task_by_id(s.data["features"][0]["spec_task"])["space"] = "sp1"
            s.dirty = True
        folder = self.loc["folder"]
        with open(settings_mod.SETTINGS, "w") as f:
            json.dump({"spaces": {"sp1": {"folder": folder, "tasks": {"risky_paths": ["config/*.json"]}},
                                  "other": {"folder": "/some/other/folder", "tasks": {"risky_paths": ["nowhere/*"]}}}}, f)
        self.addCleanup(lambda: os.path.exists(settings_mod.SETTINGS) and os.remove(settings_mod.SETTINGS))
        pkg = self.plan()["packages"][0]
        self.assertEqual(pkg["risk"], "high")  # the feature-space risky path applied
        self.assertEqual(pkg["merge"], "ask")


# ---- the openspec validate plan guard (C7/C8) ------------------------------------------------

@unittest.skipUnless(shutil.which("openspec"), "openspec CLI not installed")
class CheckPlanTest(Base):
    def setUp(self):
        super().setUp()
        # check_plan runs in the spec worker's own worktree: the change sits in the working tree here
        # (self.repo), not yet merged to the feature branch, so write it there and read with folder=.
        self.make_feature()
        subprocess.run(["openspec", "init", "--tools", "none"], cwd=self.repo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.cdir = os.path.join(self.repo, "openspec", "changes", self.change)
        os.makedirs(os.path.join(self.cdir, "specs", "auth"), exist_ok=True)
        with open(os.path.join(self.cdir, "proposal.md"), "w") as f:
            f.write("## Why\nwhy\n\n## What Changes\n- a\n\n## Impact\n- auth\n")
        self.write_worktree_table(self.repo, VALID_TABLE)
        self._spec(valid=True)

    def _spec(self, valid):
        body = ("## ADDED Requirements\n\n### Requirement: Login\nThe system SHALL let a user log in.\n\n"
                "#### Scenario: Valid\n- **WHEN** valid\n- **THEN** ok\n") if valid else \
               ("## ADDED Requirements\n\n### Requirement: Login\nNo scenario and no keyword here.\n")
        with open(os.path.join(self.cdir, "specs", "auth", "spec.md"), "w") as f:
            f.write(body)

    def test_valid_change_passes(self):
        self.assertEqual(fanout.check_plan(self.repo, self.change), {"ok": True, "detail": ""})

    def test_invalid_openspec_change_fails(self):
        self._spec(valid=False)
        r = fanout.check_plan(self.repo, self.change)
        self.assertFalse(r["ok"])
        self.assertTrue(r["detail"])

    def test_bad_work_table_fails(self):
        # The OpenSpec change validates, but the work table has a cycle: the plan guard still refuses.
        self.write_worktree_table(
            self.repo,
            "## Work table\n\n| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | A | a.ts | 0.2 | low | S |\n| 0.2 | B | b.ts | 0.1 | low | S |\n")
        r = fanout.check_plan(self.repo, self.change)
        self.assertFalse(r["ok"])


# ---- the command entry (C4) ------------------------------------------------------------------

class Args:
    def __init__(self, **kw):
        self.change = None
        self.folder = None
        self.json = False
        self.dry_run = False
        self.amend = False
        self.machines = None
        self.no_start = False
        self.__dict__.update(kw)


class CmdFanoutTest(Base):
    def setUp(self):
        super().setUp()
        self.make_feature()
        self.write_table(VALID_TABLE)

    def _capture(self, args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fanout.cmd_fanout(args)
        return rc, buf.getvalue()

    def test_dry_run_json_creates_nothing(self):
        before = self.read()
        rc, out = self._capture(Args(change=self.change, folder=self.repo, json=True, dry_run=True))
        self.assertEqual(rc, 0)
        p = json.loads(out)
        self.assertTrue(p["valid"])
        self.assertEqual(len(p["packages"]), 4)
        self.assertEqual(self.read(), before)  # nothing created

    def test_real_run_creates(self):
        # --no-start: cmd_fanout only wakes the scheduler when it queued tasks, and a test must not
        # touch the real scheduler terminal (it is not isolated by the throwaway store). The start path
        # itself is covered by RunTest against fanout.run.
        rc, out = self._capture(Args(change=self.change, folder=self.repo, json=True, no_start=True))
        self.assertEqual(rc, 0)
        p = json.loads(out)
        self.assertEqual(p["created"]["children"], 4)
        children = [t for t in self.read()["tasks"] if t.get("role") == "child"]
        self.assertEqual(len(children), 4)
        self.assertTrue(all(t["state"] == "todo" for t in children))  # created, not started

    def test_invalid_table_exit_1(self):
        self.write_table("## Work table\n\nnothing\n")
        rc, out = self._capture(Args(change=self.change, folder=self.repo, dry_run=True))
        self.assertEqual(rc, 1)

    def test_real_run_invalid_prints_c5_shape(self):
        # A real run (not dry) with a bad table still prints the C5 invalid shape and exits 1, rather
        # than losing it to a plain {"error": ...}; nothing is created.
        self.write_table(
            "## Work table\n\n| id | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
            "| 0.1 | A | a.ts | 0.2 | low | S |\n| 0.2 | B | b.ts | 0.1 | low | S |\n")
        rc, out = self._capture(Args(change=self.change, folder=self.repo, json=True))
        self.assertEqual(rc, 1)
        p = json.loads(out)
        self.assertFalse(p["valid"])
        self.assertEqual(p["code"], "cycle")
        self.assertEqual(len([t for t in self.read()["tasks"] if t.get("role") == "child"]), 0)


if __name__ == "__main__":
    unittest.main()
