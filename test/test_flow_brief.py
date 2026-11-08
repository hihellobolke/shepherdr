"""The OpenSpec flow file that brief() adds to a Feature Task's prompt (docs/openspec-flow.md,
section 9.3 T15).

When the Settings block says Flow: openspec and the Task has a flow role, brief() puts one flow file
right after the normal worker words: the spec Task gets flow-spec, a Child, Fix or Integration Task
gets flow-task, the Verify Task gets flow-verify. A classic Task, and an ordinary Task in an openspec
space, get none: the classic brief stays byte for byte the same as before. A flow Task in a repo with
no openspec/ folder stops with a clear message.

The flow files are read from the real templates/ of this repo, so the frontmatter stripping is tested
against the files workers actually get. instructions() is faked so the test never rewrites the real
herdr-core templates on disk."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

import herdr_task.launch as launch  # noqa: E402
import herdr_task.settings as settings  # noqa: E402
from herdr_task.base import Fail  # noqa: E402

TEMPLATES = os.path.join(ROOT, "templates")

# A line from the body of each real flow file (after its frontmatter), to prove the right file landed.
SPEC_LINE = "OpenSpec flow: your Task is the spec Task of a Feature."
TASK_LINE = "OpenSpec flow: your Task is a Child Task of a Feature."
VERIFY_LINE = "OpenSpec flow: your Task is the Verify Task of a Feature."


class Store:
    """Enough of herdr_task.store.Store for brief()."""
    def __init__(self, folder):
        self.loc = {"folder": folder, "kind": "repo", "name": "f"}

    def ref(self, t):
        return f"SHEP-{t['id']}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "repo")
        os.makedirs(self.folder)
        self.settings_file = os.path.join(self.tmp, "shepherdr.json")
        patches = [
            mock.patch.object(settings, "SETTINGS", self.settings_file),
            # flow_file reads the real templates/ of this repo; nothing is written there.
            mock.patch.object(launch, "TEMPLATES", TEMPLATES),
            # instructions() is faked so the test never touches the real herdr-core templates.
            mock.patch.object(launch, "instructions", lambda role: f"{role.upper()}-WORDS"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.s = Store(self.folder)

    def openspec(self):
        os.makedirs(os.path.join(self.folder, "openspec"), exist_ok=True)

    def write_settings(self, d):
        import json
        with open(self.settings_file, "w") as f:
            json.dump(d, f)

    def task(self, **kw):
        return {"id": 1, "space": "sp1", "title": "Login flow", "prompt": "Build the login flow.", **kw}

    def brief(self, t):
        return launch.brief(self.s, t, space="sp1", argv=["claude"])


class FlowFile(Base):
    def test_strips_the_frontmatter(self):
        body = launch.flow_file("flow-spec")
        self.assertTrue(body.startswith(SPEC_LINE), body[:80])
        self.assertNotIn("---", body.split("\n")[0])
        self.assertNotIn("name: flow-spec", body)

    def test_missing_file_is_none(self):
        self.assertIsNone(launch.flow_file("flow-nope"))


class FlowPart(Base):
    def test_classic_task_gets_nothing(self):
        self.openspec()
        self.assertIsNone(launch.flow_part(self.s, self.task(role="child"), "sp1"))

    def test_openspec_without_a_flow_role_gets_nothing(self):
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        self.assertIsNone(launch.flow_part(self.s, self.task(), "sp1"))

    def test_each_role_gets_its_file(self):
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        cases = {"spec": SPEC_LINE, "child": TASK_LINE, "fix": TASK_LINE,
                 "integration": TASK_LINE, "verify": VERIFY_LINE}
        for role, first in cases.items():
            part = launch.flow_part(self.s, self.task(role=role), "sp1")
            self.assertIsNotNone(part, role)
            self.assertTrue(part.startswith(first), (role, part[:80]))

    def test_flow_set_per_task_also_counts(self):
        self.openspec()
        part = launch.flow_part(self.s, self.task(role="spec", set={"flow": "openspec"}), "sp1")
        self.assertTrue(part.startswith(SPEC_LINE))

    def test_flow_task_without_openspec_folder_stops(self):
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        with self.assertRaises(Fail) as e:
            launch.flow_part(self.s, self.task(role="spec"), "sp1")
        msg = str(e.exception)
        self.assertIn("openspec/", msg)
        self.assertIn("flow init", msg)

    def test_classic_task_without_openspec_folder_does_not_stop(self):
        # No openspec/ folder and no flow: the guard must not fire for a classic task.
        self.assertIsNone(launch.flow_part(self.s, self.task(role="child"), "sp1"))

    def test_missing_flow_template_stops(self):
        # openspec/ is there, but the flow file is not: a flow Task must not launch with no role
        # rules, so brief() stops clearly instead of silently dropping the file.
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        empty = os.path.join(self.tmp, "no-templates")
        os.makedirs(empty)
        with mock.patch.object(launch, "TEMPLATES", empty):
            with self.assertRaises(Fail) as e:
                launch.flow_part(self.s, self.task(role="spec"), "sp1")
        self.assertIn("flow-spec.md", str(e.exception))


class BriefInjection(Base):
    def test_classic_brief_is_byte_for_byte_unchanged(self):
        """A classic Task's brief equals the brief as if the flow injection did not exist."""
        t = self.task()
        real = self.brief(t)
        with mock.patch.object(launch, "flow_part", lambda *a, **k: None):
            baseline = self.brief(t)
        self.assertEqual(real, baseline)
        self.assertNotIn("OpenSpec flow", real)

    def test_classic_brief_equals_the_known_component_join(self):
        """The classic brief is exactly the pre-flow join of its parts, in order, with no flow slot."""
        from herdr_task.settings import notes, notes_part, settings_block
        t = self.task()
        s = self.s
        start = notes_part("Before you start", notes(s, t, "start", "sp1"))
        body = "Your task, " + s.ref(t) + ":\n" + ((t.get("prompt") or "").strip() or t["title"])
        finish = notes_part("Before you say ready", notes(s, t, "finish", "sp1"))
        expected = "\n\n".join(x for x in [
            launch.instructions("worker"), start, body, launch.where_line(t), finish,
            launch.closing_line(s, t, None, ["claude"]), settings_block(s, t, "sp1", ["claude"])] if x)
        self.assertEqual(self.brief(t), expected)

    def test_openspec_without_a_role_is_also_unchanged(self):
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        t = self.task()
        real = self.brief(t)
        with mock.patch.object(launch, "flow_part", lambda *a, **k: None):
            baseline = self.brief(t)
        self.assertEqual(real, baseline)
        self.assertNotIn("OpenSpec flow", real)

    def test_spec_brief_has_the_file_right_after_the_worker_words(self):
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        body = launch.flow_file("flow-spec")
        brief = self.brief(self.task(role="spec"))
        self.assertTrue(brief.startswith("WORKER-WORDS\n\n" + body + "\n\n"), brief[:200])
        # The flow file comes before the task body.
        self.assertLess(brief.index(SPEC_LINE), brief.index("Your task, SHEP-1"))

    def test_child_and_verify_briefs_get_their_files(self):
        self.openspec()
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        self.assertIn(TASK_LINE, self.brief(self.task(role="child")))
        self.assertIn(VERIFY_LINE, self.brief(self.task(role="verify")))

    def test_spec_brief_without_openspec_folder_stops(self):
        self.write_settings({"everywhere": {"tasks": {"flow": "openspec"}}})
        with self.assertRaises(Fail):
            self.brief(self.task(role="spec"))


if __name__ == "__main__":
    unittest.main()
