"""retro.py: the retro Task for a Feature (proposal 6.15, contract C1/C4/C7).

The `tasks.retro` setting decides whether a retro runs (`off`, `ask`, `automatic`); the
`tasks.retro_save` setting decides where its report goes (`outside`, `repo`, `pr`); the
`tasks.retro_prompt` setting decides the instructions (`built-in`, a skill name, a file path);
and the retro agent is the `retro` role, else the Reviewer's. Nothing happens when Retro is off.

The Store and the repo are real throwaway files (AGENTS.md: throwaway roots, real throwaway git
repos). Settings are a real throwaway shepherdr.json, patched in. Nothing shells out here: `start`
only creates and queues the Task record; the ordinary queue starts it later.
"""

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
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.dont_write_bytecode = True
if BIN not in sys.path:
    sys.path.insert(0, BIN)


class RetroBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        os.environ["HERDR_TASK_STORE"] = os.path.join(self.tmp, "store")
        from herdr_task import store, retro, settings, base
        self.store, self.retro, self.settings, self.base = store, retro, settings, base
        # A real throwaway settings file, patched in so settings_for reads it.
        self.file = os.path.join(self.tmp, "shepherdr.json")
        p = mock.patch.object(settings, "SETTINGS", self.file)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("HERDR_TASK_STORE", None)

    def write(self, d):
        with open(self.file, "w") as f:
            json.dump(d, f)

    def everywhere(self, tasks=None, retro=None):
        d = {"everywhere": {}}
        if tasks is not None:
            d["everywhere"]["tasks"] = tasks
        if retro is not None:
            d["everywhere"]["retro"] = retro
        self.write(d)

    def _store(self):
        return self.store.Store(self.store.locate(self.repo))

    def _feature(self, s, title="Login flow", **kw):
        return s.new_feature(title, kw.pop("change", "add-login-flow"),
                             kw.pop("branch", "feature/REPO-1-login"), **kw)


class RetroAgent(RetroBase):
    def test_retro_role_is_used(self):
        self.everywhere(retro={"agent": "codex", "model": "gpt-x", "effort": "high"})
        with self._store() as s:
            self.assertEqual(self.retro.retro_agent(s),
                             {"agent": "codex", "model": "gpt-x", "effort": "high"})

    def test_reviewers_when_retro_is_empty(self):
        self.write({"everywhere": {"reviewer": {"agent": "codex", "model": "gpt-x"}}})
        with self._store() as s:
            self.assertEqual(self.retro.retro_agent(s),
                             {"agent": "codex", "model": "gpt-x", "effort": None})

    def test_none_values_when_neither_is_set(self):
        with self._store() as s:
            self.assertEqual(self.retro.retro_agent(s),
                             {"agent": None, "model": None, "effort": None})


class InstructionsLine(RetroBase):
    def test_built_in(self):
        line = self.retro._instructions_line("built-in")
        self.assertIn("writing-for-agents", line)
        self.assertIn("retro skill", line)

    def test_none_is_built_in(self):
        self.assertEqual(self.retro._instructions_line(None), self.retro._instructions_line("built-in"))

    def test_skill_name(self):
        self.assertEqual(self.retro._instructions_line("my-retro"), "Follow the `my-retro` skill for the retro.")

    def test_file_path(self):
        for path in ("docs/retro.md", "/abs/x.md", "~/notes.md", "./local.md"):
            self.assertEqual(self.retro._instructions_line(path), f"Follow the retro instructions in {path}.")


class FeatureSpace(RetroBase):
    def test_spec_tasks_space(self):
        with self._store() as s:
            feat = self._feature(s)
            spec = s.new("Spec", feature=feat["id"], role="spec")
            spec["space"] = "sp-spec"
            feat["spec_task"] = spec["id"]
            child = s.new("Child", feature=feat["id"], role="child")
            child["space"] = "sp-child"
            self.assertEqual(self.retro._feature_space(s, feat), "sp-spec")

    def test_falls_back_to_a_child(self):
        with self._store() as s:
            feat = self._feature(s)
            child = s.new("Child", feature=feat["id"], role="child")
            child["space"] = "sp-child"
            self.assertEqual(self.retro._feature_space(s, feat), "sp-child")

    def test_none_when_no_tasks(self):
        with self._store() as s:
            feat = self._feature(s)
            self.assertIsNone(self.retro._feature_space(s, feat))


class StartOff(RetroBase):
    def test_off_default_creates_nothing(self):
        with self._store() as s:
            feat = self._feature(s)
            before = len(s.data["tasks"])
            self.assertIsNone(self.retro.start(s, feat))
            self.assertEqual(len(s.data["tasks"]), before)

    def test_off_explicit_creates_nothing(self):
        self.everywhere(tasks={"retro": "off"})
        with self._store() as s:
            feat = self._feature(s)
            self.assertIsNone(self.retro.start(s, feat))
            self.assertEqual(s.data["tasks"], [])

    def test_forced_runs_even_when_off(self):
        self.everywhere(tasks={"retro": "off"})
        with self._store() as s:
            feat = self._feature(s)
            t = self.retro.start(s, feat, mode="automatic")
            self.assertIsNotNone(t)
            self.assertEqual(t["role"], "retro")


class StartCreates(RetroBase):
    def _start(self, tasks=None, retro=None, mode=None):
        if tasks is not None or retro is not None:
            self.everywhere(tasks=tasks, retro=retro)
        with self._store() as s:
            feat = self._feature(s)
            t = self.retro.start(s, feat, mode=mode)
            return s, feat, t

    def test_automatic_creates_a_queued_retro_task(self):
        s, feat, t = self._start(tasks={"retro": "automatic"})
        self.assertEqual(t["role"], "retro")
        self.assertEqual(t["feature"], feat["id"])
        self.assertEqual(t["state"], "queued")
        self.assertIn("queued_at", t)
        self.assertIn(t, s.data["tasks"])

    def test_ask_also_creates(self):
        _, _, t = self._start(tasks={"retro": "ask"})
        self.assertIsNotNone(t)
        self.assertEqual(t["role"], "retro")

    def test_review_and_proof_are_off(self):
        _, _, t = self._start(tasks={"retro": "automatic"})
        self.assertEqual(t["set"]["review"], "off")
        self.assertEqual(t["set"]["proof"], "off")

    def test_prompt_names_the_feature_and_data_command(self):
        s, feat, t = self._start(tasks={"retro": "automatic"})
        ref = s.ref(feat)
        self.assertIn(ref, t["prompt"])
        self.assertIn(f"retro-data {ref}", t["prompt"])
        self.assertIn("read-only", t["prompt"])

    def test_agent_from_retro_role(self):
        _, _, t = self._start(tasks={"retro": "automatic"}, retro={"agent": "codex", "effort": "high"})
        self.assertEqual(t["agent"], "codex")
        self.assertEqual(t["effort"], "high")
        self.assertNotIn("model", t)  # not set in the role, so not stamped

    def test_no_agent_fields_when_none_configured(self):
        _, _, t = self._start(tasks={"retro": "automatic"})
        for k in ("agent", "model", "effort"):
            self.assertNotIn(k, t)

    def test_save_outside_is_the_default_and_uses_the_folder(self):
        _, _, t = self._start(tasks={"retro": "automatic"})
        self.assertIn("outside the repo", t["prompt"])
        self.assertEqual(t["mode"], "here")

    def test_save_repo_mentions_docs_retros_and_uses_a_worktree(self):
        _, _, t = self._start(tasks={"retro": "automatic", "retro_save": "repo"})
        self.assertIn("docs/retros/", t["prompt"])
        self.assertEqual(t["mode"], "worktree")

    def test_save_pr_mentions_the_pull_request(self):
        _, _, t = self._start(tasks={"retro": "automatic", "retro_save": "pr"})
        self.assertIn("pull request", t["prompt"])
        self.assertEqual(t["mode"], "here")

    def test_prompt_built_in_by_default(self):
        _, _, t = self._start(tasks={"retro": "automatic"})
        self.assertIn("writing-for-agents", t["prompt"])

    def test_prompt_skill_name(self):
        _, _, t = self._start(tasks={"retro": "automatic", "retro_prompt": "my-retro"})
        self.assertIn("Follow the `my-retro` skill", t["prompt"])

    def test_prompt_file_path(self):
        _, _, t = self._start(tasks={"retro": "automatic", "retro_prompt": "docs/retro.md"})
        self.assertIn("Follow the retro instructions in docs/retro.md", t["prompt"])

    def test_bad_save_value_falls_back_to_outside(self):
        _, _, t = self._start(tasks={"retro": "automatic", "retro_save": "nonsense"})
        self.assertIn("outside the repo", t["prompt"])
        self.assertEqual(t["mode"], "here")


class StartSpace(RetroBase):
    def test_task_takes_the_features_space(self):
        self.everywhere(tasks={"retro": "automatic"})
        with self._store() as s:
            feat = self._feature(s)
            child = s.new("Child", feature=feat["id"], role="child")
            child["space"] = "sp9"
            t = self.retro.start(s, feat)
            self.assertEqual(t["space"], "sp9")
            self.assertEqual(t["queue"], {"space": "sp9"})


class Command(RetroBase):
    def _args(self, ref, **kw):
        kw.setdefault("json", True)
        return types.SimpleNamespace(id=ref, folder=self.repo, **kw)

    def _run(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.retro.cmd_retro(args)
        return json.loads(buf.getvalue())

    def test_envelope_shape(self):
        self.everywhere(tasks={"retro": "automatic", "retro_save": "repo"})
        with self._store() as s:
            feat = self._feature(s)
            s.dirty = True
            ref, prefix = s.ref(feat), s.prefix
        data = self._run(self._args(ref))
        self.assertTrue(data["ok"])
        self.assertTrue(data["task"].startswith(f"{prefix}-"))
        self.assertEqual(data["mode"], "automatic")
        self.assertEqual(data["saved_to"], "repo")

    def test_command_forces_even_when_off(self):
        # off is the default: the command still starts a retro, and the envelope mode says off.
        with self._store() as s:
            feat = self._feature(s)
            s.dirty = True
            ref = s.ref(feat)
        data = self._run(self._args(ref))
        self.assertEqual(data["mode"], "off")
        self.assertIsNotNone(data["task"])
        # the Task is really in the store, queued, with role retro
        with self._store() as s:
            t = s.find(data["task"])
            self.assertEqual(t["role"], "retro")
            self.assertEqual(t["state"], "queued")

    def test_default_saved_to_is_outside(self):
        self.everywhere(tasks={"retro": "ask"})
        with self._store() as s:
            feat = self._feature(s)
            s.dirty = True
            ref = s.ref(feat)
        data = self._run(self._args(ref))
        self.assertEqual(data["mode"], "ask")
        self.assertEqual(data["saved_to"], "outside")

    def test_a_task_ref_is_refused(self):
        with self._store() as s:
            t = s.new("A task")
            s.dirty = True
            ref = s.ref(t)
        with self.assertRaises(self.base.Fail):
            self._run(self._args(ref))

    def test_missing_feature_is_refused(self):
        with self._store() as s:
            s.dirty = True
            ref = f"{s.prefix}-99"
        with self.assertRaises(self.base.Fail):
            self._run(self._args(ref))

    def test_text_output_when_not_json(self):
        self.everywhere(tasks={"retro": "automatic"})
        with self._store() as s:
            feat = self._feature(s)
            s.dirty = True
            ref = s.ref(feat)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.retro.cmd_retro(self._args(ref, json=False))
        out = buf.getvalue()
        self.assertIn(ref, out)
        self.assertIn("outside", out)


if __name__ == "__main__":
    unittest.main()
