"""feature_status.py: the states and digests of a Feature's children on one machine, in one call
(proposal 6.13, contract C7 `feature_status`, the `feature-status` shape of C5).

The home machine asks each machine once in each tick, merges the answers, and an offline machine
does not stop the others (proposal R8). A throwaway STORE (tempfile) and a real throwaway Store are
used; the machine name, the per-machine call out and the digest build (which replays a log through
Node) are the only things mocked, as AGENTS.md asks."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.dont_write_bytecode = True
if BIN not in sys.path:
    sys.path.insert(0, BIN)
# STORE is read at import time; the per-test dir replaces it below.
os.environ.setdefault("HERDR_TASK_STORE", tempfile.mkdtemp(prefix="herdr-fs-import-"))

from herdr_task import feature_status, digest, store  # noqa: E402


class Args:
    """A stand-in for the parsed CLI args (the real argparse wiring is task T20a's, not here)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="herdr-fs-")
        self._saved_store = store.STORE
        store.STORE = self.tmp
        self.folder = os.path.join(self.tmp, "shop")
        os.makedirs(self.folder)
        # Never reach hc for the machine name; never replay a log through Node.
        self._orig_machine = feature_status._this_machine
        self._orig_build = digest.build
        feature_status._this_machine = lambda: "home-mac"
        self.built = []
        digest.build = lambda s, task: self._fake_digest(s, task)

    def tearDown(self):
        feature_status._this_machine = self._orig_machine
        digest.build = self._orig_build
        store.STORE = self._saved_store
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_digest(self, s, task):
        """A stand-in for `digest.build` (C5): enough fields to tell it apart, and a record of the
        build so a test can prove a stored digest was reused instead of rebuilt."""
        self.built.append(task["id"])
        return {"ref": s.ref(task), "state": "merged", "stub": True}

    def _store(self, write=True):
        return store.Store(store.locate(self.folder), write)


# ---- collect: this machine's children -------------------------------------------------------

class Collect(Base):
    def _home_feature(self, s):
        """A Feature in this (home) store with three children: two on this machine (null) and one
        on vm-b; the first home child is still running, the second has ended."""
        f = s.new_feature("Login flow", "add-login-flow", "feature/x-login")
        running = s.new("Parse headers", "", "worktree", feature=f["id"], role="child", rows=["1.1"])
        running["state"] = "running"
        done = s.new("Parse body", "", "worktree", feature=f["id"], role="child", rows=["1.2"])
        done["state"] = "done"
        remote = s.new("Wire parser", "", "worktree", feature=f["id"], role="child", rows=["2.1"])
        remote["state"] = "done"
        f["children"] = [
            {"ref": running["id"], "machine": None, "rows": ["1.1"], "role": "child"},
            {"ref": done["id"], "machine": None, "rows": ["1.2"], "role": "child"},
            {"ref": remote["id"], "machine": "vm-b", "rows": ["2.1"], "role": "child"},
        ]
        return f, running, done, remote

    def test_home_collects_only_its_own_children(self):
        with self._store() as s:
            f, running, done, remote = self._home_feature(s)
            out = feature_status.collect(s, f)
        # The home machine answers for the two children with machine null, not the vm-b one.
        self.assertIsNone(out["machine"])
        self.assertTrue(out["reachable"])
        self.assertEqual([c["ref"] for c in out["children"]],
                         [f"{s.prefix}-{running['id']}", f"{s.prefix}-{done['id']}"])

    def test_shape_matches_the_contract(self):
        with self._store() as s:
            f, _, _, _ = self._home_feature(s)
            out = feature_status.collect(s, f)
        self.assertEqual(set(out), {"feature", "machine", "reachable", "children"})
        self.assertEqual(out["feature"], s.ref(f))
        for c in out["children"]:
            self.assertEqual(set(c), {"ref", "state", "role", "rows", "digest"})

    def test_a_running_child_has_no_digest_and_an_ended_child_does(self):
        with self._store() as s:
            f, running, done, _ = self._home_feature(s)
            out = feature_status.collect(s, f)
        by_ref = {c["ref"]: c for c in out["children"]}
        self.assertEqual(by_ref[f"{s.prefix}-{running['id']}"]["state"], "running")
        self.assertIsNone(by_ref[f"{s.prefix}-{running['id']}"]["digest"])
        self.assertEqual(by_ref[f"{s.prefix}-{done['id']}"]["state"], "done")
        self.assertTrue(by_ref[f"{s.prefix}-{done['id']}"]["digest"]["stub"])
        # Only the ended child's digest was built; the running one's was not.
        self.assertEqual(self.built, [done["id"]])

    def test_an_ended_child_with_a_stored_digest_is_not_rebuilt(self):
        with self._store() as s:
            f, _, done, _ = self._home_feature(s)
            key = digest.child_key({"ref": done["id"], "machine": None})
            f["digests"] = {key: {"ref": s.ref(done), "state": "merged", "stored": True}}
            out = feature_status.collect(s, f)
        by_ref = {c["ref"]: c for c in out["children"]}
        self.assertTrue(by_ref[f"{s.prefix}-{done['id']}"]["digest"]["stored"])
        self.assertEqual(self.built, [])  # nothing rebuilt

    def test_a_skipped_child_counts_as_ended(self):
        with self._store() as s:
            f = s.new_feature("Login", "add-login", "feature/x-login")
            skipped = s.new("Skip me", "", "worktree", feature=f["id"], role="child", rows=["3.1"])
            skipped["state"] = "running"
            skipped["skipped"] = True
            f["children"] = [{"ref": skipped["id"], "machine": None, "rows": ["3.1"], "role": "child"}]
            out = feature_status.collect(s, f)
        self.assertTrue(out["children"][0]["digest"]["stub"])

    def test_the_child_ref_uses_the_feature_prefix_on_a_remote_machine(self):
        """A remote machine answers for the children that carry its name, and reads each child's
        ref with the Feature's (home) prefix, so the ref is the same on every machine. The Feature
        record is not in the remote's store, so the remote machine is detected by name."""
        with self._store() as s:
            t1 = s.new("Remote child", "", "worktree", role="child", rows=["2.1"])
            t1["state"] = "done"
            t2 = s.new("Home child", "", "worktree", role="child", rows=["1.1"])
            t2["state"] = "done"
            feature = {
                "id": 999, "ref": "HAT-12", "digests": {},
                "children": [
                    {"ref": t1["id"], "machine": "home-mac", "rows": ["2.1"], "role": "child"},
                    {"ref": t2["id"], "machine": None, "rows": ["1.1"], "role": "child"},
                ],
            }
            out = feature_status.collect(s, feature)
        # Detected as the "home-mac" machine (it does not hold the Feature record), so only the
        # child that carries "home-mac" is answered, with the home prefix in its ref.
        self.assertEqual(out["machine"], "home-mac")
        self.assertEqual([c["ref"] for c in out["children"]], [f"HAT-{t1['id']}"])


# ---- _ask_machine: one call out to one machine ----------------------------------------------

class FakeRun:
    """Records the argv of each `subprocess.run`, and answers from a queue of (returncode, stdout)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        rc, out = self.answers.pop(0)
        if isinstance(rc, Exception):
            raise rc
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()


class AskMachine(Base):
    def _patch_run(self, fake):
        self._orig_run = feature_status.subprocess.run
        feature_status.subprocess.run = fake
        self.addCleanup(lambda: setattr(feature_status.subprocess, "run", self._orig_run))

    def test_it_runs_feature_status_on_that_machine(self):
        answer = {"feature": "HAT-12", "machine": None, "reachable": True,
                  "children": [{"ref": "HAT-15", "state": "done", "role": "child", "rows": ["2.2"], "digest": None}]}
        fake = FakeRun([(0, json.dumps(answer))])
        self._patch_run(fake)
        out = feature_status._ask_machine("HAT-12", "vm-b")
        # Exactly the one call, the feature-status command run on vm-b (contract C4, proposal 6.13).
        self.assertEqual(len(fake.calls), 1)
        argv = fake.calls[0]
        self.assertEqual(argv[1:], [feature_status.ENTRY, "--on", "vm-b", "feature-status", "HAT-12", "--json"])
        self.assertEqual(out["machine"], "vm-b")  # stamped with the machine asked
        self.assertTrue(out["reachable"])
        self.assertEqual(out["children"][0]["ref"], "HAT-15")

    def test_a_timeout_is_unreachable(self):
        fake = FakeRun([(feature_status.subprocess.TimeoutExpired("cmd", 60), "")])
        self._patch_run(fake)
        out = feature_status._ask_machine("HAT-12", "vm-b")
        self.assertEqual(out, {"feature": "HAT-12", "machine": "vm-b", "reachable": False, "children": []})

    def test_a_nonzero_exit_is_unreachable(self):
        fake = FakeRun([(1, "")])
        self._patch_run(fake)
        out = feature_status._ask_machine("HAT-12", "vm-b")
        self.assertFalse(out["reachable"])
        self.assertEqual(out["children"], [])

    def test_unreadable_output_is_unreachable(self):
        fake = FakeRun([(0, "not json")])
        self._patch_run(fake)
        self.assertFalse(feature_status._ask_machine("HAT-12", "vm-b")["reachable"])

    def test_an_error_envelope_is_unreachable(self):
        fake = FakeRun([(0, json.dumps({"error": "No feature HAT-12"}))])
        self._patch_run(fake)
        self.assertFalse(feature_status._ask_machine("HAT-12", "vm-b")["reachable"])


# ---- gather: one call per machine, offline does not stop the others --------------------------

class Gather(Base):
    def _patch_ask(self, by_machine):
        """Replace the per-machine call out with a recorder; each machine answers from `by_machine`,
        and an answer that is an Exception is raised (a machine that is wholly offline)."""
        self.asked = []

        def fake(ref, machine, timeout=feature_status.REMOTE_TIMEOUT):
            self.asked.append(machine)
            ans = by_machine.get(machine)
            if isinstance(ans, Exception):
                raise ans
            return ans or feature_status._unreachable(ref, machine)

        self._orig_ask = feature_status._ask_machine
        feature_status._ask_machine = fake
        self.addCleanup(lambda: setattr(feature_status, "_ask_machine", self._orig_ask))

    def _feature_on_three_machines(self, s):
        """A Feature with one child on this machine, one on vm-b and one on vm-c."""
        f = s.new_feature("Login", "add-login", "feature/x-login")
        local = s.new("Local child", "", "worktree", feature=f["id"], role="child", rows=["1.1"])
        local["state"] = "done"
        f["children"] = [
            {"ref": local["id"], "machine": None, "rows": ["1.1"], "role": "child"},
            {"ref": 101, "machine": "vm-b", "rows": ["2.1"], "role": "child"},
            {"ref": 102, "machine": "vm-c", "rows": ["2.2"], "role": "child"},
        ]
        return f, local

    def _remote_answer(self, ref, machine, child_ref, state="done"):
        return {"feature": ref, "machine": machine, "reachable": True,
                "children": [{"ref": child_ref, "state": state, "role": "child", "rows": [], "digest": None}]}

    def test_one_call_per_other_machine_and_the_results_merge(self):
        with self._store() as s:
            f, local = self._feature_on_three_machines(s)
            ref = s.ref(f)
            self._patch_ask({
                "vm-b": self._remote_answer(ref, "vm-b", "HAT-101"),
                "vm-c": self._remote_answer(ref, "vm-c", "HAT-102"),
            })
            out = feature_status.gather(s, f, [None, "vm-b", "vm-c"])
        # One call for each other machine; the local machine is not called out to.
        self.assertEqual(self.asked, ["vm-b", "vm-c"])
        # The local child and both remote children are merged into one list.
        self.assertEqual({c["ref"] for c in out["children"]},
                         {f"{s.prefix}-{local['id']}", "HAT-101", "HAT-102"})
        self.assertEqual(out["unreachable"], [])

    def test_an_offline_machine_does_not_stop_the_others(self):
        with self._store() as s:
            f, local = self._feature_on_three_machines(s)
            ref = s.ref(f)
            self._patch_ask({
                "vm-b": feature_status._unreachable(ref, "vm-b"),   # offline
                "vm-c": self._remote_answer(ref, "vm-c", "HAT-102"),
            })
            out = feature_status.gather(s, f, ["vm-b", "vm-c"])
        # vm-b was still asked, vm-c still answered, and the local child is still present.
        self.assertEqual(self.asked, ["vm-b", "vm-c"])
        self.assertEqual(out["unreachable"], ["vm-b"])
        self.assertIn("HAT-102", {c["ref"] for c in out["children"]})
        self.assertIn(f"{s.prefix}-{local['id']}", {c["ref"] for c in out["children"]})

    def test_a_machine_that_is_asked_only_once_even_if_repeated(self):
        with self._store() as s:
            f, _ = self._feature_on_three_machines(s)
            ref = s.ref(f)
            self._patch_ask({"vm-b": self._remote_answer(ref, "vm-b", "HAT-101")})
            feature_status.gather(s, f, ["vm-b", "vm-b", None, "local", ""])
        self.assertEqual(self.asked, ["vm-b"])  # deduped; the local tokens are skipped

    def test_the_local_machine_is_collected_without_a_call_out(self):
        with self._store() as s:
            f, local = self._feature_on_three_machines(s)
            self._patch_ask({})  # no remote answers available
            out = feature_status.gather(s, f, [])
        self.assertEqual(self.asked, [])
        self.assertEqual([c["ref"] for c in out["children"]], [f"{s.prefix}-{local['id']}"])


# ---- the command entry ----------------------------------------------------------------------

class Command(Base):
    def _run(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            feature_status.cmd_feature_status(args)
        return buf.getvalue()

    def test_local_prints_this_machines_children(self):
        with self._store() as s:
            f = s.new_feature("Login", "add-login", "feature/x-login")
            child = s.new("Child", "", "worktree", feature=f["id"], role="child", rows=["1.1"])
            child["state"] = "done"
            f["children"] = [{"ref": child["id"], "machine": None, "rows": ["1.1"], "role": "child"}]
            s.dirty = True
            ref = s.ref(f)
        out = self._run(Args(id=ref, folder=self.folder, on=None, json=True))
        data = json.loads(out)
        self.assertEqual(data["feature"], ref)
        self.assertIsNone(data["machine"])
        self.assertEqual(data["children"][0]["ref"], f"{ref.rsplit('-', 1)[0]}-{child['id']}")

    def test_with_on_it_asks_that_machine(self):
        answer = {"feature": "HAT-12", "machine": None, "reachable": True, "children": []}
        captured = {}

        def fake_ask(ref, machine, timeout=feature_status.REMOTE_TIMEOUT):
            captured["ref"], captured["machine"] = ref, machine
            return {**answer, "machine": machine}

        orig = feature_status._ask_machine
        feature_status._ask_machine = fake_ask
        self.addCleanup(lambda: setattr(feature_status, "_ask_machine", orig))
        out = self._run(Args(id="HAT-12", folder=self.folder, on="vm-b", json=True))
        self.assertEqual((captured["ref"], captured["machine"]), ("HAT-12", "vm-b"))
        self.assertEqual(json.loads(out)["machine"], "vm-b")


if __name__ == "__main__":
    unittest.main()
