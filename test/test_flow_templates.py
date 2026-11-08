"""SHMD-13: the worker and reviewer templates get a flow pointer that is active only when the
Settings block says Flow: openspec, the reviewer reads CODING_STANDARDS.md when present, the
Assistant template gets an OpenSpec flow section, and the old built-in texts are in the upgrade
chain so an unchanged old copy upgrades to today's while an edited copy stays."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

from herdr_task import templates  # noqa: E402
from herdr_task.templates import FRONT  # noqa: E402


def read(path):
    with open(path) as f:
        return f.read()


def body_of(role):
    text = read(os.path.join(ROOT, "templates", f"{role}.md"))
    m = FRONT.match(text)
    return text[m.end():] if m else text


class FlowPointers(unittest.TestCase):
    def test_worker_pointer_is_in_the_file_and_the_constant(self):
        worker = body_of("worker")
        for words in (worker, templates.WORKER_MD):
            # active only when the Settings block says Flow: openspec
            self.assertIn("Flow: openspec", words)
            self.assertIn("flow file (spec, child or verify)", words)
            self.assertIn("herdr-task sync-branch", words)
            # an ordinary Task in an openspec space has no flow file: work as usual (proposal 6.2)
            self.assertIn("ordinary task", words)
        self.assertIn("A brief with no flow file is an ordinary task", worker)

    def test_reviewer_pointer_and_coding_standards(self):
        reviewer = body_of("reviewer")
        for words in (reviewer, templates.REVIEWER_MD):
            self.assertIn("Flow: openspec", words)
            self.assertIn("owned paths", words)
            self.assertIn("Verify Task checks the Feature as a whole", words)
            # an ordinary Task has no owned paths: review it as usual (proposal 6.2)
            self.assertIn("review it as usual", words)
            self.assertIn("CODING_STANDARDS.md", words)
        # the reviewer.md file and the REVIEWER_MD constant stay in sync
        self.assertEqual(reviewer.strip(), templates.REVIEWER_MD.strip())

    def test_assistant_has_an_openspec_flow_section(self):
        assistant = body_of("assistant")
        self.assertIn("## OpenSpec flow", assistant)
        for words in ("feature add", "Merge feature", "Add a fix round",
                      "herdr-task feature merge", "herdr-task feature fix-round",
                      "digest", "Needs you", "Plan approval",
                      # verify findings become Fix Tasks and a new Verify Task (proposal 6.11)
                      "--role fix", "--role verify", "one fix round"):
            self.assertIn(words, assistant)
        # inert by default: it names the condition
        self.assertIn("applies only when Flow is openspec", assistant)

    def test_no_em_dash(self):
        for role in ("worker", "reviewer", "assistant"):
            self.assertNotIn("—", body_of(role), f"{role}.md has an em dash")


class UpgradeChain(unittest.TestCase):
    """An unchanged old copy upgrades to today's; an edited copy stays."""

    def test_the_old_texts_are_in_the_chain(self):
        self.assertIn(templates.WORKER_MD_BEFORE_FLOW, templates.OLD_WORKERS)
        self.assertIn(templates.REVIEWER_MD_BEFORE_FLOW, templates.OLD_REVIEWERS)
        # today's is the pre-flow text plus the pointer, so the two differ
        self.assertNotEqual(templates.WORKER_MD, templates.WORKER_MD_BEFORE_FLOW)
        self.assertNotEqual(templates.REVIEWER_MD, templates.REVIEWER_MD_BEFORE_FLOW)

    def upgrades(self, role, old, now):
        d = tempfile.mkdtemp()
        path = os.path.join(d, f"{role}.md")
        with open(path, "w") as f:
            f.write(templates.TEMPLATE_HEADS[role] + old)
        with mock.patch.object(templates, "TEMPLATES", d):
            # an unchanged old copy upgrades to today's
            self.assertEqual(templates.instructions(role), now.strip())
            # the file on disk is rewritten to today's
            self.assertIn(now.strip(), read(path))
            # an edited copy stays
            with open(path, "w") as f:
                f.write(templates.TEMPLATE_HEADS[role] + "My own words.")
            self.assertEqual(templates.instructions(role), "My own words.")

    def test_an_unchanged_old_worker_upgrades_an_edited_one_stays(self):
        self.upgrades("worker", templates.WORKER_MD_BEFORE_FLOW, templates.WORKER_MD)

    def test_an_unchanged_old_reviewer_upgrades_an_edited_one_stays(self):
        self.upgrades("reviewer", templates.REVIEWER_MD_BEFORE_FLOW, templates.REVIEWER_MD)


if __name__ == "__main__":
    unittest.main()
