"""Ready guards (T06): the owned-paths check with its exempt list, and the patch-id of the whole
diff against the base with the match rule for review validity (proposal sections 6.8 and 6.9).

The git tests use a real throwaway repo (tempfile); nothing here mocks git.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

from herdr_task import ready_guards as rg
from herdr_task.base import Fail


class OwnedPathsCheck(unittest.TestCase):
    """owned_paths_check and _matches: inside, outside, exempt files, directories and globs."""

    def test_a_changed_path_in_the_owned_paths_is_inside(self):
        r = rg.owned_paths_check(["src/parser/types.ts"], ["src/parser/types.ts"])
        self.assertEqual(r, {"ok": True, "outside": []})

    def test_a_changed_path_outside_the_owned_paths_is_listed(self):
        r = rg.owned_paths_check(
            ["src/parser/types.ts", "src/other/body.ts"], ["src/parser/types.ts"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["outside"], ["src/other/body.ts"])

    def test_outside_keeps_the_input_order(self):
        r = rg.owned_paths_check(["z.ts", "a.ts"], [])
        self.assertEqual(r["outside"], ["z.ts", "a.ts"])

    def test_a_directory_owns_entry_covers_the_files_under_it(self):
        r = rg.owned_paths_check(
            ["mods/tasks/bin/herdr_task/ready_guards.py"], ["mods/tasks/bin/herdr_task"])
        self.assertTrue(r["ok"])

    def test_a_directory_owns_entry_with_a_trailing_slash_also_covers_it(self):
        r = rg.owned_paths_check(["pkg/a.py", "pkg/sub/b.py"], ["pkg/"])
        self.assertEqual(r, {"ok": True, "outside": []})

    def test_a_directory_owns_entry_does_not_leak_to_a_sibling_prefix(self):
        # "src/par" must not cover "src/parser/..." (the segment boundary is respected).
        r = rg.owned_paths_check(["src/parser/types.ts"], ["src/par"])
        self.assertFalse(r["ok"])

    def test_a_single_star_glob_stays_within_one_segment(self):
        r = rg.owned_paths_check(
            ["src/parser/types.ts", "src/parser/sub/deep.ts"], ["src/parser/*.ts"])
        self.assertEqual(r["outside"], ["src/parser/sub/deep.ts"])

    def test_lockfiles_are_exempt_by_default(self):
        for lock in ("bun.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                     "Cargo.lock", "poetry.lock", "uv.lock"):
            r = rg.owned_paths_check([lock], ["src/only.ts"])
            self.assertTrue(r["ok"], lock)

    def test_generated_files_are_exempt_by_default(self):
        changed = ["web/generated/model.ts", "web/generated/a/b/deep.ts", "src/x.generated.ts",
                   "a/b/c.generated.js", "top.generated.css"]
        r = rg.owned_paths_check(changed, [])
        self.assertEqual(r, {"ok": True, "outside": []})

    def test_a_non_generated_file_is_not_caught_by_the_generated_glob(self):
        r = rg.owned_paths_check(["src/x.ts"], [])
        self.assertFalse(r["ok"])

    def test_the_caller_can_extend_the_exempt_list(self):
        r = rg.owned_paths_check(["schema/out.json"], ["src/only.ts"], exempt=["schema/**"])
        self.assertTrue(r["ok"])

    def test_the_exempt_argument_adds_to_the_defaults_not_replaces_them(self):
        r = rg.owned_paths_check(["bun.lock", "schema/out.json"], [], exempt=["schema/**"])
        self.assertTrue(r["ok"])

    def test_the_default_exempt_list_is_the_contract_list(self):
        self.assertEqual(rg.DEFAULT_EXEMPT, [
            "bun.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
            "poetry.lock", "uv.lock", "web/generated/**", "**/*.generated.*"])

    def test_the_reason_codes_are_the_contract_codes(self):
        self.assertEqual(rg.OWNED_PATHS, 2)
        self.assertEqual(rg.REVIEW_STALE, 4)


class ReviewValid(unittest.TestCase):
    """review_valid: a round matches on the commit hash or the patch-id (C2, section 6.9)."""

    def test_a_round_with_the_same_commit_hash_is_valid(self):
        task = {"reviews": [{"head": "abc123", "patch_id": "old"}]}
        self.assertTrue(rg.review_valid(task, "abc123", "new"))

    def test_a_round_with_the_same_patch_id_is_valid(self):
        task = {"reviews": [{"head": "old", "patch_id": "pid1"}]}
        self.assertTrue(rg.review_valid(task, "newcommit", "pid1"))

    def test_no_matching_round_is_stale(self):
        task = {"reviews": [{"head": "old", "patch_id": "pid1"}]}
        self.assertFalse(rg.review_valid(task, "newcommit", "pid2"))

    def test_a_task_with_no_rounds_is_stale(self):
        self.assertFalse(rg.review_valid({}, "c", "p"))

    def test_an_empty_commit_and_patch_never_match(self):
        task = {"reviews": [{"head": "", "patch_id": ""}]}
        self.assertFalse(rg.review_valid(task, "", ""))

    def test_a_later_round_can_be_the_matching_one(self):
        task = {"reviews": [{"head": "one", "patch_id": "p1"}, {"head": "two", "patch_id": "p2"}]}
        self.assertTrue(rg.review_valid(task, "two", "nope"))


class GitGuards(unittest.TestCase):
    """changed_paths and patch_id against a real throwaway repo: inside and outside owned paths, a
    clean rebase that keeps the patch-id, and a changed patch-id."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.write("base.txt", "base\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, content):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        with open(path, "w") as f:
            f.write(content)

    def worker_branch(self):
        self.git("checkout", "-q", "-b", "worker")
        self.write("owned.txt", "v1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "worker change")

    def test_changed_paths_lists_the_diff_against_the_base(self):
        self.worker_branch()
        self.assertEqual(rg.changed_paths(self.repo, "main"), ["owned.txt"])

    def test_changed_paths_holds_only_our_own_changes_when_the_base_moved_ahead(self):
        self.worker_branch()
        self.git("checkout", "-q", "main")
        self.write("extra.txt", "theirs\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "unrelated base commit")
        self.git("checkout", "-q", "worker")
        # Three-dot against the merge-base: the base's own new file is not ours.
        self.assertEqual(rg.changed_paths(self.repo, "main"), ["owned.txt"])

    def test_changed_paths_feeds_the_owned_paths_check(self):
        self.worker_branch()
        changed = rg.changed_paths(self.repo, "main")
        self.assertTrue(rg.owned_paths_check(changed, ["owned.txt"])["ok"])
        self.assertEqual(
            rg.owned_paths_check(changed, ["other.txt"])["outside"], ["owned.txt"])

    def test_changed_paths_raises_on_a_bad_base(self):
        self.worker_branch()
        with self.assertRaises(Fail):
            rg.changed_paths(self.repo, "no-such-branch")

    def test_patch_id_of_an_empty_diff_is_empty(self):
        self.assertEqual(rg.patch_id(self.repo, "main"), "")

    def test_patch_id_of_a_real_diff_is_a_stable_hash(self):
        self.worker_branch()
        pid = rg.patch_id(self.repo, "main")
        self.assertTrue(pid)
        self.assertEqual(pid, rg.patch_id(self.repo, "main"))  # same diff, same id

    def test_a_clean_rebase_keeps_the_patch_id(self):
        self.worker_branch()
        before = rg.patch_id(self.repo, "main")
        # The base moves ahead with an unrelated file, then the worker rebases cleanly onto it.
        self.git("checkout", "-q", "main")
        self.write("extra.txt", "theirs\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "unrelated base commit")
        self.git("checkout", "-q", "worker")
        self.git("rebase", "-q", "main")
        self.assertEqual(rg.patch_id(self.repo, "main"), before)

    def test_a_changed_diff_changes_the_patch_id(self):
        self.worker_branch()
        before = rg.patch_id(self.repo, "main")
        self.write("owned.txt", "v2 different content\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "-m", "worker change v2")
        self.assertNotEqual(rg.patch_id(self.repo, "main"), before)

    def test_review_valid_tracks_the_patch_id_across_a_clean_rebase(self):
        # The recorded round's patch-id stays valid after a clean rebase (the whole point of 6.9).
        self.worker_branch()
        pid = rg.patch_id(self.repo, "main")
        head = self.git("rev-parse", "HEAD")
        task = {"reviews": [{"head": head, "patch_id": pid}]}
        self.git("checkout", "-q", "main")
        self.write("extra.txt", "theirs\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "unrelated base commit")
        self.git("checkout", "-q", "worker")
        self.git("rebase", "-q", "main")
        new_head = self.git("rev-parse", "HEAD")
        new_pid = rg.patch_id(self.repo, "main")
        self.assertNotEqual(new_head, head)  # the commit hash changed
        self.assertTrue(rg.review_valid(task, new_head, new_pid))  # but the patch-id kept it valid


if __name__ == "__main__":
    unittest.main()
