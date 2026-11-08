"""The OpenSpec flow hooks in commands.py (T20b, contract C8).

Each hook is exercised from the CLI entry (cmd_add, cmd_ready, cmd_reviewed, cmd_accept) in a
throwaway root, with a real throwaway git repo and worktree; only hc and the flow modules that land
in parallel (feature.py, fanout.py) are mocked. ready_guards.py is on this branch and runs for real.

A classic Task carries none of the flow fields of C2, so every hook is a no-op for it; the classic
behaviour is pinned here too.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")


class FlowHooks(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, BIN)
        import herdr_task.commands as commands
        import herdr_task.settings as settings
        import herdr_task.store as store
        import herdr_task.ready_guards as ready_guards
        from herdr_task.base import Fail
        self.commands, self.settings, self.store_mod, self.ready_guards, self.Fail = \
            commands, settings, store, ready_guards, Fail

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = os.path.join(self.tmp, "app")
        os.makedirs(self.folder)
        self._git("init", "-q", "-b", "main", cwd=self.folder)
        self.write(self.folder, "src/mine.py", "mine\n")
        self.write(self.folder, "src/other.py", "other\n")
        self._git("add", "-A", cwd=self.folder)
        self._commit(self.folder, "base")

        patches = [
            mock.patch.object(store, "STORE", os.path.join(self.tmp, "tasks")),
            mock.patch.object(settings, "SETTINGS", os.path.join(self.tmp, "settings.json")),
            mock.patch.object(settings, "worker_argv", lambda s, t, space=None, key=None: ["claude"]),
            mock.patch.object(commands, "worker_argv", lambda s, t, space=None, key=None: ["claude"]),
            mock.patch.object(commands, "live_agents", lambda key: []),
            mock.patch.object(commands, "default_space", lambda: "sp1"),
            mock.patch.object(commands, "caller_space", lambda: None),
            mock.patch.object(commands, "find_proof", lambda *a, **k: None),
            mock.patch.object(commands, "busy_again", lambda *a, **k: False),
            mock.patch.object(commands, "github", lambda s: None),
            mock.patch.object(commands, "already_merged", lambda s, t: None),
            mock.patch.dict(os.environ, {"HC_TERMINAL_ID": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # ---- git and store helpers ------------------------------------------------------------

    def _git(self, *args, cwd):
        subprocess.run(["git", "-C", cwd, *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _commit(self, cwd, msg):
        subprocess.run(["git", "-C", cwd, "-c", "user.name=T", "-c", "user.email=t@e",
                        "commit", "-q", "-m", msg], check=True)

    def write(self, cwd, rel, text):
        p = os.path.join(cwd, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def head(self, cwd):
        return subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                              check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

    def feature_branch(self):
        """A feature branch off main, the child Tasks' base."""
        self._git("branch", "feature/f", "main", cwd=self.folder)

    def worktree(self, branch="task/child"):
        """A real worktree on `branch`, branched from feature/f (the child's base)."""
        wt = os.path.join(self.tmp, branch.replace("/", "-"))
        self._git("worktree", "add", "-q", "-b", branch, wt, "feature/f", cwd=self.folder)
        return wt

    def store(self, write=True):
        return self.store_mod.Store(self.store_mod.locate(self.folder), write)

    def make_feature(self):
        with self.store() as s:
            f = s.new_feature("Login flow", "add-login-flow", "feature/f", base="main")
            return s.ref(f), f["id"]

    def put_task(self, flow_fields=None, **extra):
        """A Task on disk with the given flow fields and extra record fields; returns (ref, id)."""
        with self.store() as s:
            t = s.new("A task", "", extra.pop("mode", "worktree"), **(flow_fields or {}))
            t.update(extra)
            s.dirty = True
            return s.ref(t), t["id"]

    def task(self, task_id):
        with self.store(write=False) as s:
            return s.task_by_id(task_id)

    def ns(self, **kw):
        base = dict(folder=self.folder, json=True, id=None, title=None, prompt=None, prompt_file=None,
                    here=False, worktree=False, after=None, space=None, at=None, in_=None, agent=None,
                    model=None, effort=None, reviewer=None, reviewer_model=None, reviewer_effort=None,
                    argv=None, parent=None, now=False, base=None, branch=None, start_notes=None,
                    finish_notes=None, set=None, summary=None, details=None, proof=None, agents_md=None,
                    report=None, feature=None, role=None, owns=None, risk=None)
        return argparse.Namespace(**{**base, **kw})

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(self.ns(**kw))
        text = out.getvalue()
        try:
            return json.loads(text)
        except ValueError:
            return text

    def fake_module(self, name, **funcs):
        """Install a fake flow module (feature.py / fanout.py land in parallel) so a lazy
        `from . import <name>` finds it; removed on cleanup, both from sys.modules and as an
        attribute of the package, so nothing leaks into another test."""
        import herdr_task
        mod = types.ModuleType(f"herdr_task.{name}")
        for k, v in funcs.items():
            setattr(mod, k, v)
        sys.modules[f"herdr_task.{name}"] = mod

        def clean():
            sys.modules.pop(f"herdr_task.{name}", None)
            if getattr(herdr_task, name, None) is mod:
                delattr(herdr_task, name)
        self.addCleanup(clean)
        return mod

    # ---- cmd_add (C2, C8) -----------------------------------------------------------------

    def test_add_stores_the_flow_fields(self):
        ref, fid = self.make_feature()
        self.call(self.commands.cmd_add, title="Parse body", here=True,
                  feature=ref, role="child", owns=["src/body.ts"], risk="low")
        t = self.task(fid + 1)
        self.assertEqual(t["feature"], fid)
        self.assertEqual(t["role"], "child")
        self.assertEqual(t["owns"], ["src/body.ts"])
        self.assertEqual(t["risk"], "low")

    def test_add_defaults_role_to_child(self):
        ref, fid = self.make_feature()
        self.call(self.commands.cmd_add, title="Child", here=True, feature=ref)
        self.assertEqual(self.task(fid + 1)["role"], "child")

    def test_add_high_risk_sets_merge_ask(self):
        ref, fid = self.make_feature()
        self.call(self.commands.cmd_add, title="Risky", here=True, feature=ref, role="child",
                  owns=["src/x.ts"], risk="high")
        t = self.task(fid + 1)
        self.assertEqual(t["risk"], "high")
        self.assertEqual((t.get("set") or {}).get("merge"), "ask")
        with self.store() as s:
            self.assertEqual(self.commands.merge_policy(s, t), "ask")

    def test_add_high_risk_does_not_override_an_explicit_merge(self):
        ref, fid = self.make_feature()
        self.call(self.commands.cmd_add, title="Risky", here=True, feature=ref, role="child",
                  owns=["src/x.ts"], risk="high", set=["merge=auto"])
        self.assertEqual((self.task(fid + 1).get("set") or {}).get("merge"), "auto")

    def test_add_role_requires_feature(self):
        with self.assertRaises(self.Fail):
            self.commands.cmd_add(self.ns(title="Bad", here=True, role="child"))

    def test_add_owns_accumulate_and_dedupe(self):
        ref, fid = self.make_feature()
        self.call(self.commands.cmd_add, title="Many", here=True, feature=ref,
                  owns=["a.ts, b.ts", "b.ts", "c.ts"])
        self.assertEqual(self.task(fid + 1)["owns"], ["a.ts", "b.ts", "c.ts"])

    def test_classic_add_stores_no_flow_fields(self):
        self.call(self.commands.cmd_add, title="Classic", here=True)
        t = self.task(1)
        for k in ("feature", "role", "owns", "risk"):
            self.assertNotIn(k, t)
        self.assertFalse(self.commands.is_flow(t))

    # ---- cmd_ready owned paths (C6 code 2) ------------------------------------------------

    def _child_with_commit(self, rel, owns):
        """A running flow child whose worktree has one commit changing `rel`."""
        self.feature_branch()
        _, fid = self.make_feature()
        wt = self.worktree()
        start = self.head(wt)
        self.write(wt, rel, "changed\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        ref, tid = self.put_task({"feature": fid, "role": "child", "owns": owns},
                                 mode="worktree", worktree=wt, branch="task/child", base="feature/f",
                                 state="running", started=1, start=start, review_gate=True,
                                 set={"review": "on", "review_rounds": 2})
        return ref, tid, wt

    def test_ready_refuses_paths_outside_owns(self):
        ref, tid, wt = self._child_with_commit("src/other.py", ["src/mine.py"])
        with self.assertRaises(SystemExit) as cm:
            self.call(self.commands.cmd_ready, id=ref, summary="done")
        self.assertEqual(cm.exception.code, self.ready_guards.OWNED_PATHS)

    def test_ready_refuse_prints_the_reason_code(self):
        ref, tid, wt = self._child_with_commit("src/other.py", ["src/mine.py"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            self.commands.cmd_ready(self.ns(id=ref, summary="done"))
        got = json.loads(out.getvalue())
        self.assertEqual(got["reason"], "owned-paths")
        self.assertIn("src/other.py", got["error"])

    def test_ready_allows_paths_inside_owns(self):
        ref, tid, wt = self._child_with_commit("src/mine.py", ["src/mine.py"])
        with self.store() as s:
            self.assertIsNone(self.commands.flow_ready_guards(s, s.find(ref), wt))

    def test_owned_paths_check_exempts_lockfiles(self):
        self.feature_branch()
        _, fid = self.make_feature()
        wt = self.worktree()
        start = self.head(wt)
        self.write(wt, "src/mine.py", "x\n")
        self.write(wt, "bun.lock", "lock\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        ref, _ = self.put_task({"feature": fid, "role": "child", "owns": ["src/mine.py"]},
                               mode="worktree", worktree=wt, branch="task/child", base="feature/f",
                               state="running", started=1, start=start)
        with self.store() as s:
            self.assertIsNone(self.commands.flow_ready_guards(s, s.find(ref), wt))

    def test_flow_guards_are_a_noop_for_a_classic_task(self):
        self.feature_branch()
        wt = self.worktree()
        self.write(wt, "src/other.py", "changed\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        ref, _ = self.put_task(None, mode="worktree", worktree=wt, branch="task/child",
                               base="feature/f", state="running", started=1, start=self.head(wt))
        with self.store() as s:
            self.assertIsNone(self.commands.flow_ready_guards(s, s.find(ref), wt))

    # ---- cmd_ready plan check (C6 code 3) -------------------------------------------------

    def _spec_task(self):
        self.feature_branch()
        _, fid = self.make_feature()
        wt = self.worktree(branch="task/spec")
        start = self.head(wt)
        self.write(wt, "openspec/changes/add-login-flow/tasks.md", "# plan\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "spec")
        ref, tid = self.put_task({"feature": fid, "role": "spec"},
                                 mode="worktree", worktree=wt, branch="task/spec", base="feature/f",
                                 state="running", started=1, start=start, review_gate=True,
                                 set={"review": "on", "review_rounds": 2})
        return ref, tid, wt

    def test_ready_refuses_an_invalid_plan_for_a_spec_task(self):
        ref, tid, wt = self._spec_task()
        self.fake_module("fanout", check_plan=lambda folder, change: {"ok": False, "detail": "no work table"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            self.commands.cmd_ready(self.ns(id=ref, summary="done"))
        self.assertEqual(cm.exception.code, self.commands.PLAN_INVALID)
        got = json.loads(out.getvalue())
        self.assertEqual(got["reason"], "plan-invalid")
        self.assertIn("no work table", got["error"])

    def test_ready_allows_a_valid_plan(self):
        ref, tid, wt = self._spec_task()
        seen = {}
        def check_plan(folder, change):
            seen["folder"], seen["change"] = folder, change
            return {"ok": True, "detail": ""}
        self.fake_module("fanout", check_plan=check_plan)
        with self.store() as s:
            self.assertIsNone(self.commands.flow_ready_guards(s, s.find(ref), wt))
        self.assertEqual(seen["change"], "add-login-flow")
        self.assertEqual(seen["folder"], wt)

    # ---- cmd_reviewed stores the patch-id (C8) --------------------------------------------

    def test_reviewed_stores_the_patch_id_for_a_flow_task(self):
        ref, tid, wt = self._child_with_commit("src/mine.py", ["src/mine.py"])
        self.call(self.commands.cmd_reviewed, id=ref, report="looks good")
        rnd = self.task(tid)["reviews"][-1]
        self.assertEqual(rnd["head"], self.head(wt))
        self.assertEqual(rnd["patch_id"], self.ready_guards.patch_id(wt, "feature/f"))
        self.assertTrue(rnd["patch_id"])

    def test_classic_reviewed_stores_no_patch_id(self):
        self.feature_branch()
        wt = self.worktree()
        self.write(wt, "src/mine.py", "x\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        ref, tid = self.put_task(None, mode="worktree", worktree=wt, branch="task/child",
                                 base="feature/f", state="running", started=1, start=self.head(wt),
                                 set={"review": "on", "review_rounds": 2})
        self.call(self.commands.cmd_reviewed, id=ref, report="ok")
        self.assertNotIn("patch_id", self.task(tid)["reviews"][-1])

    # ---- review validity across a clean rebase (C6 code 4) --------------------------------

    def test_review_stays_valid_across_a_clean_rebase(self):
        ref, tid, wt = self._child_with_commit("src/mine.py", ["src/mine.py"])
        self.call(self.commands.cmd_reviewed, id=ref, report="ok")
        # Amend the commit: a new hash, the same tree, so the same patch-id.
        subprocess.run(["git", "-C", wt, "-c", "user.name=T", "-c", "user.email=t@e",
                        "commit", "--amend", "-q", "-m", "work (amended)"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self.store() as s:
            t = s.find(ref)
            t["head"] = self.head(wt)
            self.assertIsNone(self.commands.review_refusal(s, t, wt))

    def test_review_goes_stale_after_a_content_change(self):
        ref, tid, wt = self._child_with_commit("src/mine.py", ["src/mine.py"])
        self.call(self.commands.cmd_reviewed, id=ref, report="ok")
        self.write(wt, "src/mine.py", "different\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "more work")
        with self.store() as s:
            t = s.find(ref)
            t["head"] = self.head(wt)
            refused = self.commands.review_refusal(s, t, wt)
        self.assertIsNotNone(refused)
        _msg, reason, code = refused
        self.assertEqual(reason, "review-stale")
        self.assertEqual(code, self.ready_guards.REVIEW_STALE)

    def test_ready_exits_four_on_a_stale_review(self):
        ref, tid, wt = self._child_with_commit("src/mine.py", ["src/mine.py"])
        # No review round recorded: the final commit has none that matches.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            self.commands.cmd_ready(self.ns(id=ref, summary="done"))
        self.assertEqual(cm.exception.code, self.ready_guards.REVIEW_STALE)
        self.assertEqual(json.loads(out.getvalue())["reason"], "review-stale")

    def test_classic_review_refusal_carries_no_reason_code(self):
        self.feature_branch()
        wt = self.worktree()
        start = self.head(wt)  # the base tip, before any commit of its own
        self.write(wt, "src/mine.py", "x\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        ref, tid = self.put_task(None, mode="worktree", worktree=wt, branch="task/child",
                                 base="feature/f", state="running", started=1, start=start,
                                 head=self.head(wt), review_gate=True,
                                 set={"review": "on", "review_rounds": 2})
        with self.store() as s:
            refused = self.commands.review_refusal(s, s.find(ref), wt)
        self.assertIsNotNone(refused)
        _msg, reason, code = refused
        self.assertIsNone(reason)
        self.assertEqual(code, 1)

    def test_classic_review_accepts_a_matching_commit_hash(self):
        self.feature_branch()
        wt = self.worktree()
        self.write(wt, "src/mine.py", "x\n")
        self._git("add", "-A", cwd=wt)
        self._commit(wt, "work")
        h = self.head(wt)
        ref, tid = self.put_task(None, mode="worktree", worktree=wt, branch="task/child",
                                 base="feature/f", state="running", started=1, start="start-sha",
                                 head=h, review_gate=True, set={"review": "on", "review_rounds": 2},
                                 reviews=[{"round": 1, "result": "report", "at": 2, "head": h}])
        with self.store() as s:
            self.assertIsNone(self.commands.review_refusal(s, s.find(ref), wt))

    # ---- cmd_accept Plan approval hook (C8) ----------------------------------------------

    def test_accept_runs_the_plan_approval_hook_for_a_spec_task(self):
        _, fid = self.make_feature()
        ref, tid = self.put_task({"feature": fid, "role": "spec"}, mode="here", state="ready")
        called = []
        self.fake_module("feature", on_spec_accepted=lambda s, t: called.append(t["id"]))
        self.call(self.commands.cmd_accept, id=ref)
        self.assertEqual(called, [tid])
        self.assertEqual(self.task(tid)["state"], "done")

    def test_accept_skips_the_hook_for_a_classic_task(self):
        ref, tid = self.put_task(None, mode="here", state="ready")
        self.fake_module("feature", on_spec_accepted=lambda s, t: self.fail("hook ran for a classic task"))
        self.call(self.commands.cmd_accept, id=ref)
        self.assertEqual(self.task(tid)["state"], "done")

    def test_accept_skips_the_hook_for_a_non_spec_child(self):
        _, fid = self.make_feature()
        ref, tid = self.put_task({"feature": fid, "role": "child"}, mode="here", state="ready")
        self.fake_module("feature", on_spec_accepted=lambda s, t: self.fail("hook ran for a child task"))
        self.call(self.commands.cmd_accept, id=ref)
        self.assertEqual(self.task(tid)["state"], "done")


if __name__ == "__main__":
    unittest.main()
