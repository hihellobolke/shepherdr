"""`flow init` (herdr_task/flow_init.py): openspec init, the skill check at repo and user level,
the ask only when a skill is missing at both, the pinned copy with the MIT notice, the commit as a
pull request, and `--update`. A real throwaway git repo is used; OpenSpec and the network (the
Matt Pocock repo) are mocked, so nothing is fetched or run for real."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

import herdr_task.flow_init as flow_init  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class FlowInit(unittest.TestCase):
    def setUp(self):
        import shutil
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

        # The repo flow init prepares: a real throwaway git repo with one commit.
        self.folder = os.path.join(self.root, "repo")
        os.makedirs(self.folder)
        git(self.folder, "init", "-q", "-b", "main")
        git(self.folder, "config", "user.name", "T")
        git(self.folder, "config", "user.email", "t@example.com")
        git(self.folder, "config", "commit.gpgsign", "false")
        with open(os.path.join(self.folder, "README.md"), "w") as f:
            f.write("repo\n")
        git(self.folder, "add", "-A")
        git(self.folder, "commit", "-q", "-m", "first")

        # A fake user HOME, so ~/.claude/skills is a throwaway.
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self._patch(flow_init.base, "HOME", self.home)

        # A fake Matt Pocock skills repo, so the copy never reaches the network.
        self.source = self._fake_source(["retro", "grilling", "to-spec", "domain-modeling",
                                         "grill-with-docs", "writing-for-agents"])
        self._patch(flow_init, "_REPO_CACHE", {})
        self._patch(flow_init, "_repo_at", lambda commit: self.source)

        # OpenSpec, mocked: it writes an openspec/ folder instead of running the real CLI.
        def fake_openspec(folder):
            os.makedirs(os.path.join(folder, "openspec"), exist_ok=True)
            with open(os.path.join(folder, "openspec", "project.md"), "w") as fh:
                fh.write("# project\n")
        self._patch(flow_init, "_openspec_init", fake_openspec)

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _fake_source(self, names):
        """A throwaway Matt Pocock repo: skills under categories, plus an MIT LICENSE."""
        src = os.path.join(self.root, "mp-skills")
        with open_dir(os.path.join(src, "LICENSE")) as f:
            f.write("MIT License\n\nCopyright (c) 2026 Matt Pocock\n")
        cats = {"grilling": "productivity", "writing-for-agents": "productivity"}
        for name in names:
            cat = cats.get(name, "engineering")
            with open_dir(os.path.join(src, "skills", cat, name, "SKILL.md")) as f:
                f.write(f"# {name}\n\nDo {name}.\n")
        return src

    def repo_skill(self, name):
        """Put a skill in the repo's .claude/skills (repo level)."""
        with open_dir(os.path.join(self.folder, ".claude", "skills", name, "SKILL.md")) as f:
            f.write(f"# {name} (in repo)\n")

    def user_skill(self, name):
        """Put a skill in the fake ~/.claude/skills (user level)."""
        with open_dir(os.path.join(self.home, ".claude", "skills", name, "SKILL.md")) as f:
            f.write(f"# {name} (user)\n")

    # ---- skill_present ----------------------------------------------------------------------

    def test_skill_present_reads_repo_then_user_then_none(self):
        self.repo_skill("retro")
        self.user_skill("grilling")
        self.assertEqual(flow_init.skill_present("retro", self.folder), "repo")
        self.assertEqual(flow_init.skill_present("grilling", self.folder), "user")
        self.assertIsNone(flow_init.skill_present("to-spec", self.folder))

    def test_skill_present_prefers_repo_over_user(self):
        self.repo_skill("retro")
        self.user_skill("retro")
        self.assertEqual(flow_init.skill_present("retro", self.folder), "repo")

    # ---- the ask: repo, user and neither ----------------------------------------------------

    def test_ask_happens_only_for_a_skill_missing_at_both_levels(self):
        self.repo_skill("retro")
        self.user_skill("grilling")  # "to-spec" is at neither level
        self._patch(flow_init, "SKILLS", ["retro", "grilling", "to-spec"])
        asked = []

        def confirm(name):
            asked.append(name)
            return True

        report = flow_init.init(self.folder, confirm=confirm)
        # The ask fired for the missing-at-both skill only.
        self.assertEqual(asked, ["to-spec"])
        self.assertEqual(report["skills"], {"retro": "repo", "grilling": "user", "to-spec": "added"})

    def test_a_declined_skill_is_skipped_not_copied(self):
        self._patch(flow_init, "SKILLS", ["to-spec"])
        report = flow_init.init(self.folder, confirm=lambda name: False)
        self.assertEqual(report["skills"], {"to-spec": "skipped"})
        self.assertFalse(os.path.isdir(os.path.join(self.folder, ".claude", "skills", "to-spec")))

    def test_an_accepted_skill_is_copied_pinned_with_the_mit_notice(self):
        self._patch(flow_init, "SKILLS", ["to-spec"])
        flow_init.init(self.folder, confirm=lambda name: True)
        dst = os.path.join(self.folder, ".claude", "skills", "to-spec")
        self.assertTrue(os.path.isfile(os.path.join(dst, "SKILL.md")))
        with open(os.path.join(dst, "LICENSE")) as f:
            self.assertIn("MIT License", f.read())
        with open(os.path.join(dst, flow_init.SOURCE_MARKER)) as f:
            marker = json.load(f)
        self.assertEqual(marker["commit"], flow_init.MATT_POCOCK_COMMIT)
        self.assertEqual(marker["name"], "to-spec")
        self.assertEqual(marker["repo"], flow_init.MATT_POCOCK_REPO)

    # ---- openspec init ----------------------------------------------------------------------

    def test_openspec_is_initialized_when_absent(self):
        self._patch(flow_init, "SKILLS", [])
        report = flow_init.init(self.folder, confirm=lambda name: False)
        self.assertEqual(report["openspec"], "initialized")
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "openspec")))
        # The openspec folder is committed on the branch for review, even with no skill copied.
        self.assertEqual(report["pr"], flow_init.INIT_BRANCH)
        self.assertIn("openspec/project.md", git(self.folder, "ls-files").splitlines())

    def test_openspec_is_present_when_already_there(self):
        os.makedirs(os.path.join(self.folder, "openspec"))
        self._patch(flow_init, "SKILLS", [])
        report = flow_init.init(self.folder, confirm=lambda name: False)
        self.assertEqual(report["openspec"], "present")

    # ---- the commit as a pull request -------------------------------------------------------

    def test_a_change_is_committed_on_a_branch_for_review(self):
        self._patch(flow_init, "SKILLS", ["to-spec"])
        report = flow_init.init(self.folder, confirm=lambda name: True)
        self.assertEqual(report["pr"], flow_init.INIT_BRANCH)
        self.assertEqual(git(self.folder, "rev-parse", "--abbrev-ref", "HEAD"), flow_init.INIT_BRANCH)
        # The copied skill and the openspec folder are in the commit.
        tracked = git(self.folder, "ls-files").splitlines()
        self.assertIn(".claude/skills/to-spec/SKILL.md", tracked)
        self.assertIn("openspec/project.md", tracked)

    def test_no_change_means_no_branch(self):
        os.makedirs(os.path.join(self.folder, "openspec"))  # openspec already present
        self.repo_skill("to-spec")
        self._patch(flow_init, "SKILLS", ["to-spec"])  # the one skill is already at repo level
        report = flow_init.init(self.folder, confirm=lambda name: True)
        self.assertIsNone(report["pr"])
        self.assertEqual(report["skills"], {"to-spec": "repo"})
        self.assertEqual(git(self.folder, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    # ---- --update ---------------------------------------------------------------------------

    def test_update_moves_a_pinned_copy_and_does_not_ask(self):
        # A pinned copy already in the repo, at an older commit.
        flow_init.copy_skill("to-spec", self.folder, "0000000000000000000000000000000000000000")
        os.makedirs(os.path.join(self.folder, "openspec"))  # so update's only change is the skill
        self._patch(flow_init, "SKILLS", ["to-spec"])
        asked = []
        report = flow_init.init(self.folder, update=True, confirm=lambda name: asked.append(name) or True)
        self.assertEqual(asked, [])  # --update never asks
        self.assertEqual(report["skills"], {"to-spec": "added"})
        with open(os.path.join(self.folder, ".claude", "skills", "to-spec", flow_init.SOURCE_MARKER)) as f:
            marker = json.load(f)
        self.assertEqual(marker["commit"], flow_init.MATT_POCOCK_COMMIT)
        self.assertEqual(report["pr"], flow_init.UPDATE_BRANCH)

    def test_update_does_not_add_a_missing_skill(self):
        os.makedirs(os.path.join(self.folder, "openspec"))
        self._patch(flow_init, "SKILLS", ["to-spec"])  # missing at both levels, no pinned copy
        asked = []
        report = flow_init.init(self.folder, update=True, confirm=lambda name: asked.append(name) or True)
        self.assertEqual(asked, [])
        self.assertEqual(report["skills"], {"to-spec": "skipped"})
        self.assertFalse(os.path.isdir(os.path.join(self.folder, ".claude", "skills", "to-spec")))

    def test_update_leaves_a_hand_written_repo_skill_alone(self):
        self.repo_skill("to-spec")  # the person's own copy, no source marker
        os.makedirs(os.path.join(self.folder, "openspec"))
        self._patch(flow_init, "SKILLS", ["to-spec"])
        report = flow_init.init(self.folder, update=True, confirm=lambda name: True)
        self.assertEqual(report["skills"], {"to-spec": "repo"})
        self.assertFalse(os.path.isfile(os.path.join(self.folder, ".claude", "skills", "to-spec", flow_init.SOURCE_MARKER)))

    def test_update_skips_a_copy_already_at_the_target_commit(self):
        # A pinned copy already at the target commit is left as it is, not rewritten.
        flow_init.copy_skill("to-spec", self.folder, flow_init.MATT_POCOCK_COMMIT)
        os.makedirs(os.path.join(self.folder, "openspec"))
        self._patch(flow_init, "SKILLS", ["to-spec"])
        report = flow_init.init(self.folder, update=True, confirm=lambda name: True)
        self.assertEqual(report["skills"], {"to-spec": "repo"})
        self.assertIsNone(report["pr"])  # nothing changed, so no branch

    # ---- safe git handling ------------------------------------------------------------------

    def test_an_existing_init_branch_is_refused_not_reset(self):
        # A branch left from an earlier run must not be reset (its history is the person's).
        git(self.folder, "branch", flow_init.INIT_BRANCH)
        before = git(self.folder, "rev-parse", flow_init.INIT_BRANCH)
        self._patch(flow_init, "SKILLS", ["to-spec"])
        with self.assertRaises(flow_init.Fail):
            flow_init.init(self.folder, confirm=lambda name: True)
        self.assertEqual(git(self.folder, "rev-parse", flow_init.INIT_BRANCH), before)

    def test_unrelated_staged_work_is_not_committed(self):
        # Something already staged in the worktree must stay out of the flow init commit.
        with open(os.path.join(self.folder, "README.md"), "w") as f:
            f.write("an unrelated edit\n")
        git(self.folder, "add", "README.md")
        self._patch(flow_init, "SKILLS", ["to-spec"])
        report = flow_init.init(self.folder, confirm=lambda name: True)
        committed = git(self.folder, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertIn(".claude/skills/to-spec/SKILL.md", committed)
        self.assertNotIn("README.md", committed)
        self.assertEqual(report["pr"], flow_init.INIT_BRANCH)

    def test_a_commit_error_raises_rather_than_reporting_success(self):
        # A git failure (a hook, a missing identity) must not be reported as ok with no branch.
        self._patch(flow_init, "SKILLS", ["to-spec"])
        real_git = flow_init.base.git

        def failing(cwd, *args):
            if "commit" in args:
                return False, "", "fatal: could not write commit"
            return real_git(cwd, *args)

        self._patch(flow_init.base, "git", failing)
        with self.assertRaises(flow_init.Fail):
            flow_init.init(self.folder, confirm=lambda name: True)

    # ---- the command entry ------------------------------------------------------------------

    def test_cmd_flow_init_prints_one_json_object_with_a_missing_skill(self):
        import contextlib
        import io
        import types
        self._patch(flow_init, "SKILLS", ["to-spec"])  # missing at both levels
        args = types.SimpleNamespace(folder=self.folder, json=True, update=False)
        out = io.StringIO()
        self._patch(sys, "stdin", io.StringIO(""))  # default confirm reads stdin: EOF is a no
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            flow_init.cmd_flow_init(args)
        report = json.loads(out.getvalue())  # one clean JSON object, no prompt mixed in
        self.assertTrue(report["ok"])
        self.assertEqual(report["skills"], {"to-spec": "skipped"})

    # ---- the report shape -------------------------------------------------------------------

    def test_report_has_the_contract_shape(self):
        self.repo_skill("retro")
        self._patch(flow_init, "SKILLS", ["retro"])
        report = flow_init.init(self.folder, confirm=lambda name: False)
        self.assertEqual(set(report), {"ok", "openspec", "skills", "pr"})
        self.assertTrue(report["ok"])
        self.assertIn(report["openspec"], ("initialized", "present"))


def open_dir(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "w")


if __name__ == "__main__":
    unittest.main()
