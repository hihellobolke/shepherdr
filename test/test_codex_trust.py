"""SHMD-34: a codex reviewer or helper a worker starts in a fresh task worktree never dies on
codex's "Do you trust this directory?" prompt.

Codex (verified against codex-cli 0.167) keys its directory trust on the git repository root - for a
linked worktree, the MAIN checkout, not the worktree path - and ignores a quoted dotted-path -c key,
so the trust must be set as an inline `projects` table keyed on that root. herdr-task fills the exact
reviewer start command into the brief (codex_review_note), its own codex worker launch carries the
same trust (own_argv), and an unchanged old worker template upgrades to today's words."""
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

from herdr_task import templates  # noqa: E402
from herdr_task.templates import FRONT  # noqa: E402
from herdr_task import launch as L  # noqa: E402

# The old trust-less command that killed the reviewer at turn 0.
OLD_CODEX = "codex --sandbox read-only --ask-for-approval never"


def body_of(role):
    with open(os.path.join(ROOT, "templates", f"{role}.md")) as f:
        text = f.read()
    m = FRONT.match(text)
    return text[m.end():] if m else text


class WorkerTemplate(unittest.TestCase):
    """The built-in words and the worker.md file both tell the worker to use the trust command the
    brief fills in, and no longer carry the old trust-less command."""

    def test_the_reviewer_command_defers_to_the_brief(self):
        for words in (templates.WORKER_MD, body_of("worker")):
            # the reviewer runs codex with --no-daemon and a -c the brief supplies
            self.assertIn("codex --no-daemon -c '<the projects trust table your brief gives>'", words)
            self.assertIn("your brief fills in the exact `-c` with the repository root", words)
            # why: a fresh worktree is untrusted and the prompt kills the reviewer at turn 0
            self.assertIn("Do you trust this directory?", words)
            self.assertIn("turn 0", words)
            # the config file is never written
            self.assertIn("never edit ~/.codex/config.toml", words)
            # the old trust-less command is gone
            self.assertNotIn(OLD_CODEX, words)

    def test_the_file_and_the_constant_agree(self):
        self.assertIn(templates.TRUST_RULE, body_of("worker"))
        self.assertIn(templates.TRUST_RULE, templates.WORKER_MD)


class UpgradeChain(unittest.TestCase):
    """An unchanged old worker copy (the one just before this change) upgrades to today's; an
    edited copy stays."""

    def test_the_pre_trust_text_is_in_the_chain(self):
        self.assertIn(templates.WORKER_MD_BEFORE_TRUST, templates.OLD_WORKERS)
        self.assertNotEqual(templates.WORKER_MD, templates.WORKER_MD_BEFORE_TRUST)
        self.assertIn(OLD_CODEX, templates.WORKER_MD_BEFORE_TRUST)
        self.assertNotIn("--no-daemon", templates.WORKER_MD_BEFORE_TRUST)

    def test_an_unchanged_old_worker_upgrades_an_edited_one_stays(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "worker.md")
        with open(path, "w") as f:
            f.write(templates.TEMPLATE_HEADS["worker"] + templates.WORKER_MD_BEFORE_TRUST)
        with mock.patch.object(templates, "TEMPLATES", d):
            # an unchanged old copy upgrades to today's
            self.assertEqual(templates.instructions("worker"), templates.WORKER_MD.strip())
            with open(path) as f:
                self.assertIn("--no-daemon", f.read())
            # an edited copy stays untouched
            with open(path, "w") as f:
                f.write(templates.TEMPLATE_HEADS["worker"] + "My own words.")
            self.assertEqual(templates.instructions("worker"), "My own words.")


class TrustedRoot(unittest.TestCase):
    """codex_trusted_root resolves a linked worktree to its main checkout (what codex checks)."""

    def test_a_worktree_resolves_to_the_main_checkout(self):
        d = tempfile.mkdtemp()
        main = os.path.join(d, "repo")
        subprocess.run(["git", "init", "-q", main], check=True)
        subprocess.run(["git", "-C", main, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", main, "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", main, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        wt = os.path.join(d, "wt")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", wt, "-b", "b"], check=True)
        self.assertEqual(os.path.realpath(L.codex_trusted_root(wt)), os.path.realpath(main))
        # the worktree path itself is NOT what codex trusts
        self.assertNotEqual(os.path.realpath(L.codex_trusted_root(wt)), os.path.realpath(wt))

    def test_outside_a_repo_it_falls_back_to_the_folder(self):
        d = tempfile.mkdtemp()
        self.assertEqual(L.codex_trusted_root(d), os.path.abspath(d))


class HerdrTaskOwnLaunch(unittest.TestCase):
    """herdr-task's own codex worker launch (own_argv) carries the repo root's trust as an inline
    table, so a codex worker in a fresh worktree does not die on the trust prompt either."""

    def test_codex_launch_trusts_the_root_as_an_inline_table(self):
        with mock.patch.object(L, "codex_trusted_root", lambda c: "/tmp/the repo"):
            out = L.own_argv(["codex", "--no-daemon", "-s", "read-only", "-a", "on-request"], "/tmp/wt")
        i = out.index("-c")
        self.assertEqual(out[i + 1], 'projects={"/tmp/the repo"={trust_level="trusted"}}')
        # the agent's own sandbox flags stay in charge (trust never escalates them)
        self.assertEqual(out[-4:], ["-s", "read-only", "-a", "on-request"])

    def test_no_trust_without_a_folder(self):
        out = L.own_argv(["codex"], None)
        self.assertFalse(any("trust_level" in str(x) for x in out))

    def test_claude_launch_is_untouched_by_trust(self):
        out = L.own_argv(["claude"], "/tmp/wt")
        self.assertFalse(any("trust_level" in str(x) for x in out))


class ReviewNoteInTheBrief(unittest.TestCase):
    """codex_review_note gives the worker the exact reviewer command with the root filled in, only
    when review is on and the reviewer runs codex."""

    def _s(self):
        return SimpleNamespace(loc={"folder": "/tmp/the repo", "kind": "repo"})

    def test_a_codex_reviewer_gets_the_exact_command(self):
        with mock.patch.object(L, "review_limits", lambda s, t: (True, 2)), \
                mock.patch.object(L, "reviewer_agent", lambda s, t, sp, a: "codex"), \
                mock.patch.object(L, "codex_trusted_root", lambda c: "/tmp/the repo"):
            note = L.codex_review_note(self._s(), {"id": 1}, "sp1", ["claude"])
        self.assertIn('codex --no-daemon -c \'projects={"/tmp/the repo"={trust_level="trusted"}}\' '
                      '--sandbox read-only --ask-for-approval never', note)
        # it says the table replaces the whole projects table, fine for a read-only reviewer
        self.assertIn("replaces the whole projects table", note)
        self.assertIn("read-only reviewer", note)
        self.assertIn("never edit ~/.codex/config.toml", note)

    def test_no_note_when_the_reviewer_is_claude(self):
        with mock.patch.object(L, "review_limits", lambda s, t: (True, 2)), \
                mock.patch.object(L, "reviewer_agent", lambda s, t, sp, a: "claude"):
            self.assertEqual(L.codex_review_note(self._s(), {"id": 1}, "sp1", ["codex"]), "")

    def test_no_note_when_review_is_off(self):
        with mock.patch.object(L, "review_limits", lambda s, t: (False, 0)), \
                mock.patch.object(L, "reviewer_agent", lambda s, t, sp, a: "codex"):
            self.assertEqual(L.codex_review_note(self._s(), {"id": 1}, "sp1", ["claude"]), "")


if __name__ == "__main__":
    unittest.main()
