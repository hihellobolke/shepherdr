"""The fake `hc` and `gh` harness (`test/flowfakes.py`, task T23h): each fake behaviour the
end-to-end tests rely on, proven in isolation, and proof that nothing touches the real `hc`, `gh`
or home.

Every test builds the harness in its own throwaway directory (a bare remote, a home clone, a
scratch home) and runs the fakes as the executables a test puts on `PATH`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flowfakes
from flowfakes import FlowFakes


def read_jsonl(path):
    """The JSON objects in a `.jsonl` file (empty list when it does not exist yet)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def git(cwd, *args, check=True):
    # The same isolated git identity the harness uses, so the tests need no global git config.
    env = {**os.environ, **flowfakes._GIT_ENV}
    r = subprocess.run(["git", "-C", cwd, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fakes = FlowFakes(self.root).start()
        self.env = self.fakes.env()

    def gh(self, *args):
        return subprocess.run([os.path.join(self.fakes.bin_dir, "gh"), *args], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def hc(self, *args, stdin=None):
        return subprocess.run([os.path.join(self.fakes.bin_dir, "hc"), *args], env=self.env,
                              input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)

    def branch(self, name, *, path=None, rel="work.txt", content="work", base="main"):
        """A pushed task branch: a worktree on `name` with one commit, pushed to the origin."""
        path = path or os.path.join(self.root, "wt-" + name.replace("/", "_"))
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", name, path, base)
        with open(os.path.join(path, rel), "w") as f:
            f.write(content + "\n")
        git(path, "add", rel)
        git(path, "commit", "-q", "-m", content)
        git(path, "push", "-q", "origin", name)
        return path


# ---- the environment and the scratch world ----------------------------------------------------

class Environment(Base):
    def test_fakes_are_first_on_path_and_gh_is_pointed_at_the_fake(self):
        self.assertEqual(self.env["PATH"].split(os.pathsep)[0], self.fakes.bin_dir)
        self.assertEqual(self.env["HERDR_TASK_GH"], os.path.join(self.fakes.bin_dir, "gh"))
        # `hc` and `gh` resolve to the fakes in the scratch bin, never a real one.
        for tool in ("hc", "gh"):
            found = shutil.which(tool, path=self.env["PATH"])
            self.assertEqual(found, os.path.join(self.fakes.bin_dir, tool))

    def test_store_settings_and_state_point_into_the_scratch_root(self):
        for key in ("HERDR_TASK_STORE", "HERDR_TASK_SETTINGS", "FLOWFAKES_STATE"):
            self.assertTrue(self.env[key].startswith(self.root), f"{key}={self.env[key]}")

    def test_env_keeps_the_real_home_so_the_guard_lets_hc_run(self):
        # base.guard refuses hc when HOME is not the account's own, before it even resolves the fake
        # on PATH. So env() must not change HOME; isolation is through the redirected store/settings.
        self.assertEqual(self.env["HOME"], os.environ["HOME"])
        # An explicit home= is honoured, for tests that drive the fakes on their own.
        self.assertEqual(self.fakes.env(home="/tmp/x")["HOME"], "/tmp/x")

    def test_env_redirects_worktrees_under_the_scratch_root(self):
        self.assertTrue(self.env["HERDR_TASK_WORKTREES"].startswith(self.root))

    def test_base_honours_the_worktrees_override_so_worktrees_stay_off_the_real_home(self):
        # base.WORKTREES must read HERDR_TASK_WORKTREES, so a real herdr-task's worktrees land in the
        # scratch root, never the real ~/.herdr/worktrees (HOME stays real for base.guard).
        bin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "mods", "tasks", "bin")
        code = ("import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]);"
                "import herdr_task.base as b; print(b.WORKTREES)")
        with_override = subprocess.run([sys.executable, "-c", code, bin_dir], env=self.env,
                                       capture_output=True, text=True)
        self.assertEqual(with_override.stdout.strip(), self.env["HERDR_TASK_WORKTREES"])
        # Without the override it falls back to the real path, so the default behaviour is unchanged.
        plain = {k: v for k, v in self.env.items() if k != "HERDR_TASK_WORKTREES"}
        fallback = subprocess.run([sys.executable, "-c", code, bin_dir], env=plain,
                                  capture_output=True, text=True)
        self.assertTrue(fallback.stdout.strip().endswith(os.path.join(".herdr", "worktrees")))

    def test_the_remote_and_the_home_clone_exist_with_a_first_commit(self):
        self.assertTrue(os.path.isdir(os.path.join(self.fakes.origin, "refs")))  # a bare repo
        self.assertEqual(git(self.fakes.home_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(self.fakes.base_sha())  # main exists on the remote


# ---- gh: the repo and pull requests -----------------------------------------------------------

class GhRepo(Base):
    def test_repo_view_prints_the_name_with_owner(self):
        r = self.gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        self.assertEqual(r.stdout.strip(), "octo/demo")
        self.assertEqual(r.returncode, 0)

    def test_repo_view_json_without_jq(self):
        r = self.gh("repo", "view", "--json", "nameWithOwner")
        self.assertEqual(json.loads(r.stdout), {"nameWithOwner": "octo/demo"})


class GhPullRequests(Base):
    def test_create_list_and_view_a_pull_request(self):
        self.branch("task/HAT-14-x")
        r = self.gh("pr", "create", "--repo", "octo/demo", "--head", "task/HAT-14-x",
                    "--base", "main", "--title", "Parser", "--body", "body")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "https://github.com/octo/demo/pull/1")

        r = self.gh("pr", "list", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--state", "open",
                    "--json", "number,url,isCrossRepository,baseRefName,headRefOid")
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["number"], 1)
        self.assertFalse(rows[0]["isCrossRepository"])
        self.assertEqual(rows[0]["baseRefName"], "main")
        self.assertEqual(rows[0]["headRefOid"], self.fakes.base_sha("task/HAT-14-x"))

        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "state", "-q", ".state")
        self.assertEqual(r.stdout.strip(), "OPEN")

    def test_create_is_idempotent_for_one_branch(self):
        self.branch("task/HAT-14-x")
        for _ in range(2):
            self.gh("pr", "create", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--base", "main",
                    "--title", "T", "--body", "b")
        self.assertEqual(len(self.fakes.prs()), 1)

    def test_checks_default_to_success_and_can_be_scripted(self):
        self.branch("task/HAT-14-x")
        self.gh("pr", "create", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--base", "main",
                "--title", "T", "--body", "b")
        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "statusCheckRollup")
        rollup = json.loads(r.stdout)["statusCheckRollup"]
        self.assertEqual(rollup[0]["conclusion"], "SUCCESS")

        self.fakes.set_checks(1, "FAILURE")
        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "statusCheckRollup")
        self.assertEqual(json.loads(r.stdout)["statusCheckRollup"][0]["conclusion"], "FAILURE")

        self.fakes.set_checks(1, "PENDING")
        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "statusCheckRollup")
        rollup = json.loads(r.stdout)["statusCheckRollup"][0]
        self.assertEqual(rollup["status"], "IN_PROGRESS")
        self.assertEqual(rollup["conclusion"], "")

    def test_a_comment_is_recorded(self):
        self.branch("task/HAT-14-x")
        self.gh("pr", "create", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--base", "main",
                "--title", "T", "--body", "b")
        self.gh("pr", "comment", "1", "--repo", "octo/demo", "--body", "looks good")
        self.assertEqual(self.fakes.find_pr(number=1)["comments"], ["looks good"])

    def test_view_of_a_missing_pull_request_fails(self):
        r = self.gh("pr", "view", "99", "--repo", "octo/demo", "--json", "state")
        self.assertEqual(r.returncode, 1)

    def test_an_open_pr_head_sha_follows_a_later_push(self):
        path = self.branch("task/HAT-14-x")
        self.fakes.open_pr("task/HAT-14-x")
        # The child pushes a correction (as after a rebase).
        with open(os.path.join(path, "fix.txt"), "w") as f:
            f.write("fix\n")
        git(path, "add", "fix.txt")
        git(path, "commit", "-q", "-m", "fix")
        git(path, "push", "-q", "--force-with-lease", "origin", "task/HAT-14-x")
        new_tip = self.fakes.base_sha("task/HAT-14-x")
        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "headRefOid")
        self.assertEqual(json.loads(r.stdout)["headRefOid"], new_tip)
        # On merge the head is frozen at the merged tip.
        self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
        r = self.gh("pr", "view", "1", "--repo", "octo/demo", "--json", "headRefOid,state")
        data = json.loads(r.stdout)
        self.assertEqual(data["state"], "MERGED")
        self.assertEqual(data["headRefOid"], new_tip)


# ---- gh: squash merge into the bare remote ----------------------------------------------------

class SquashMerge(Base):
    def test_a_squash_merge_lands_one_commit_on_the_base_and_leaves_the_branch(self):
        path = self.branch("task/HAT-14-x", rel="feature.txt", content="the feature")
        before_branch = git(path, "rev-parse", "HEAD")
        self.fakes.open_pr("task/HAT-14-x", title="Add feature")

        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash", "--delete-branch")
        self.assertEqual(r.returncode, 0, r.stderr)

        pr = self.fakes.find_pr(number=1)
        self.assertEqual(pr["state"], "MERGED")
        self.assertTrue(pr["mergeCommit"])
        # The base moved by exactly one commit and now holds the feature file.
        log = git(self.fakes.origin, "log", "--oneline", "main").splitlines()
        self.assertEqual(len(log), 2)  # first + the squash commit
        self.assertIn("feature.txt", git(self.fakes.origin, "ls-tree", "--name-only", "main"))
        # The head branch on the remote is untouched by the squash (its own commits stay).
        self.assertEqual(self.fakes.base_sha("task/HAT-14-x"), before_branch)

    def test_merge_refuses_a_stale_match_head_commit(self):
        # github.cmd merges with --match-head-commit <sha> (the head it last saw); GitHub refuses when
        # the branch has moved since.
        path = self.branch("task/HAT-14-x")
        old_head = self.fakes.base_sha("task/HAT-14-x")
        self.fakes.open_pr("task/HAT-14-x")
        # The child pushes another commit, so the head moves.
        with open(os.path.join(path, "more.txt"), "w") as f:
            f.write("more\n")
        git(path, "add", "more.txt")
        git(path, "commit", "-q", "-m", "more")
        git(path, "push", "-q", "origin", "task/HAT-14-x")
        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash", "--match-head-commit", old_head)
        self.assertEqual(r.returncode, 1)
        self.assertIn("head has changed", r.stderr)
        self.assertEqual(self.fakes.find_pr(number=1)["state"], "OPEN")
        # With the current head it merges.
        new_head = self.fakes.base_sha("task/HAT-14-x")
        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash", "--match-head-commit", new_head)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fakes.find_pr(number=1)["state"], "MERGED")

    def test_a_merged_pull_request_shows_in_the_merged_list(self):
        self.branch("task/HAT-14-x")
        self.fakes.open_pr("task/HAT-14-x")
        self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
        r = self.gh("pr", "list", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--state", "merged",
                    "--json", "number,baseRefName,headRefOid,mergeCommit")
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["mergeCommit"]["oid"])
        # And it is gone from the open list.
        r = self.gh("pr", "list", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--state", "open",
                    "--json", "number")
        self.assertEqual(json.loads(r.stdout), [])


# ---- the forced-conflict scenario -------------------------------------------------------------

class ForcedConflict(Base):
    def seed_shared(self, content):
        with open(os.path.join(self.fakes.home_repo, "shared.txt"), "w") as f:
            f.write(content)
        git(self.fakes.home_repo, "add", "shared.txt")
        git(self.fakes.home_repo, "commit", "-q", "-m", "shared")
        git(self.fakes.home_repo, "push", "-q", "origin", "main")

    def test_a_conflict_child_makes_the_merge_conflict_and_leaves_the_base_alone(self):
        self.seed_shared("base line\n")
        wt = os.path.join(self.root, "wt-c")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-30-c", wt, "main")
        self.fakes.script_agent("c30", conflict=True, ready=False)
        res = self.fakes.run_agent("c30", worktree=wt, branch="task/HAT-30-c", ref="HAT-30")
        self.assertEqual(res.returncode, 0, res.stderr)
        # The base moves on, in a way that will clash with the child's shared.txt.
        git(self.fakes.home_repo, "checkout", "-q", "main")
        self.seed_shared("base changed differently\n")
        base_before = self.fakes.base_sha()
        self.fakes.open_pr("task/HAT-30-c")

        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
        self.assertEqual(r.returncode, 1)
        self.assertIn("shared.txt", r.stderr)
        pr = self.fakes.find_pr(number=1)
        self.assertEqual(pr["mergeable"], "CONFLICTING")
        self.assertEqual(pr["state"], "OPEN")  # not merged
        self.assertEqual(self.fakes.base_sha(), base_before)  # the base is untouched

    def test_a_scripted_conflicting_flag_is_sticky(self):
        self.branch("task/HAT-30-c")
        self.fakes.open_pr("task/HAT-30-c")
        self.fakes.set_mergeable(1, "CONFLICTING")
        # A scripted conflict refuses every attempt (it does not retry the git merge).
        for _ in range(2):
            r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.fakes.find_pr(number=1)["state"], "OPEN")

    def test_a_detected_conflict_recovers_once_the_head_is_resolved(self):
        self.seed_shared("base line\n")
        wt = os.path.join(self.root, "wt-c")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-30-c", wt, "main")
        self.fakes.script_agent("c30", conflict=True, ready=False)
        self.fakes.run_agent("c30", worktree=wt, branch="task/HAT-30-c", ref="HAT-30")
        git(self.fakes.home_repo, "checkout", "-q", "main")
        self.seed_shared("base changed differently\n")
        self.fakes.open_pr("task/HAT-30-c")
        # First merge hits a real git conflict.
        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.fakes.find_pr(number=1)["mergeable"], "CONFLICTING")
        # The child resolves: it merges the base in and pushes, so the head no longer conflicts.
        git(wt, "fetch", "-q", "origin", "main")
        git(wt, "-c", "core.editor=true", "merge", "-q", "-X", "ours", "origin/main")
        git(wt, "push", "-q", "--force-with-lease", "origin", "task/HAT-30-c")
        # Now the merge of the resolved head goes through (the detected conflict was not sticky).
        r = self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")
        self.assertEqual(r.returncode, 0, r.stderr)
        pr = self.fakes.find_pr(number=1)
        self.assertEqual(pr["state"], "MERGED")
        self.assertEqual(pr["mergeable"], "MERGEABLE")


# ---- the child agent --------------------------------------------------------------------------

class FakeHerdrTask:
    """A fake `herdr-task` placed in the harness bin that records each call, so a test can prove
    the agent ran `herdr-task ready`."""

    @staticmethod
    def install(fakes):
        log = os.path.join(fakes.state_dir, "herdr-task-calls.jsonl")
        path = os.path.join(fakes.bin_dir, "herdr-task")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    f"open({log!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "print('{\"ok\": true}')\n")
        os.chmod(path, 0o755)
        return log


class ChildAgent(Base):
    def test_the_agent_commits_to_its_branch_pushes_and_runs_ready(self):
        log = FakeHerdrTask.install(self.fakes)
        wt = os.path.join(self.root, "wt-a")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-20-p", wt, "main")
        self.fakes.script_agent("p20", owns="parser.txt", message="parse headers")
        res = self.fakes.run_agent("p20", worktree=wt, branch="task/HAT-20-p", ref="HAT-20",
                                   folder=self.fakes.home_repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        # It made a commit on the branch, touching its owned path.
        self.assertIn("parser.txt", git(wt, "show", "--name-only", "--format=", "HEAD"))
        # It pushed the branch to the origin.
        self.assertTrue(self.fakes.base_sha("task/HAT-20-p"))
        # It ran `herdr-task ready HAT-20`.
        self.assertEqual(read_jsonl(log), [["ready", "HAT-20", "--folder", self.fakes.home_repo, "--json"]])

    def test_a_failed_child_exits_nonzero_and_never_runs_ready(self):
        log = FakeHerdrTask.install(self.fakes)
        wt = os.path.join(self.root, "wt-f")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-21-f", wt, "main")
        self.fakes.script_agent("f21", fail=True)
        res = self.fakes.run_agent("f21", worktree=wt, branch="task/HAT-21-f", ref="HAT-21",
                                   folder=self.fakes.home_repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn("child failed", res.stderr)
        self.assertFalse(os.path.exists(log))  # ready was never reached

    def test_a_risky_child_does_its_work_but_waits_for_an_accept(self):
        wt = os.path.join(self.root, "wt-r")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-22-risky", wt, "main")
        self.fakes.script_agent("r22", risky=True, ready=False, owns="risky.txt")
        res = self.fakes.run_agent("r22", worktree=wt, branch="task/HAT-22-risky", ref="HAT-22")
        self.assertEqual(res.returncode, 0, res.stderr)
        # It committed and pushed, like any child, but is recorded as awaiting the person's Accept.
        self.assertTrue(self.fakes.base_sha("task/HAT-22-risky"))
        ran = self.fakes.ran("r22")
        self.assertTrue(ran["risky"])
        self.assertTrue(ran["awaiting_accept"])
        # A non-risky child is not awaiting an accept.
        wt2 = os.path.join(self.root, "wt-n")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-24-plain", wt2, "main")
        self.fakes.script_agent("n24", ready=False, owns="plain.txt")
        self.fakes.run_agent("n24", worktree=wt2, branch="task/HAT-24-plain", ref="HAT-24")
        self.assertFalse(self.fakes.ran("n24")["awaiting_accept"])

    def test_an_unscripted_agent_uses_the_default_behaviour(self):
        FakeHerdrTask.install(self.fakes)
        wt = os.path.join(self.root, "wt-d")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-23-d", wt, "main")
        res = self.fakes.run_agent("unscripted", worktree=wt, branch="task/HAT-23-d", ref="HAT-23",
                                   folder=self.fakes.home_repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.fakes.base_sha("task/HAT-23-d"))


# ---- hc run: starting the agent ---------------------------------------------------------------

class BranchRef(unittest.TestCase):
    def test_child_ref_is_the_last_ref_token_in_the_branch(self):
        # A child branch carries the feature ref then the child ref (C5); the child is the last one.
        self.assertEqual(flowfakes.ref_from_branch("task/HAT-12-HAT-14-parser"), "HAT-14")
        # A classic task branch and a feature branch have a single ref.
        self.assertEqual(flowfakes.ref_from_branch("task/HAT-14-title"), "HAT-14")
        self.assertEqual(flowfakes.ref_from_branch("feature/HAT-12-login-flow"), "HAT-12")
        self.assertEqual(flowfakes.ref_from_branch("no-ref-here"), "no-ref-here")


class HcRun(Base):
    def wait_for(self, predicate, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_hc_run_starts_the_agent_and_prints_its_address(self):
        FakeHerdrTask.install(self.fakes)
        wt = os.path.join(self.root, "wt-r")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-40-p", wt, "main")
        self.fakes.script_agent("task-40", owns="forty.txt")
        r = self.hc("run", "--bg", "--json", "--keep", "--cwd", wt, "--name", "task-40",
                    "--label", "herdr.task=HAT-40", "--no-parent", "--", "env", "claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["address"], "task-40")
        # The agent runs in the background and makes the branch's commit.
        self.assertTrue(self.wait_for(lambda: self.fakes.base_sha("task/HAT-40-p") is not None),
                        "the agent never pushed its branch")
        self.assertIn("forty.txt", git(wt, "show", "--name-only", "--format=", "HEAD"))

    def test_hc_run_readies_the_child_ref_from_a_feature_child_branch(self):
        log = FakeHerdrTask.install(self.fakes)
        wt = os.path.join(self.root, "wt-fc")
        git(self.fakes.home_repo, "worktree", "add", "-q", "-b", "task/HAT-12-HAT-14-parser", wt, "main")
        self.hc("run", "--bg", "--json", "--keep", "--cwd", wt, "--name", "child-14",
                "--no-parent", "--", "env", "claude")
        self.assertTrue(self.wait_for(lambda: bool(read_jsonl(log))), "the agent never ran ready")
        # ready is run for the child ref HAT-14, not the feature ref HAT-12.
        self.assertEqual(read_jsonl(log)[0][:2], ["ready", "HAT-14"])


# ---- a second machine -------------------------------------------------------------------------

class SecondMachine(Base):
    def test_hc_exec_runs_on_the_machine_store(self):
        m = self.fakes.add_machine("vm-b")
        r = self.hc("exec", "--on", "vm-b", "--", "sh", "-c", "echo $HERDR_TASK_STORE")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), m["store"])

    def test_a_machine_that_is_down_refuses_the_connection(self):
        self.fakes.add_machine("vm-b")
        self.fakes.set_down("vm-b")
        r = self.hc("exec", "--on", "vm-b", "--", "echo", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("connection refused", r.stderr)

    def test_machine_ls_lists_the_registered_machines(self):
        self.fakes.add_machine("vm-b")
        self.fakes.add_machine("vm-c")
        r = self.hc("machine", "ls", "--json")
        names = [m["name"] for m in json.loads(r.stdout)["machines"]]
        self.assertEqual(names, ["vm-b", "vm-c"])

    def test_file_write_lands_under_the_machine_home(self):
        m = self.fakes.add_machine("vm-b")
        self.hc("file", "write", "--on", "vm-b", ".local/bin/herdr-task", stdin="payload")
        dest = os.path.join(m["home"], ".local/bin/herdr-task")
        self.assertTrue(os.path.isfile(dest))
        with open(dest) as f:
            self.assertEqual(f.read(), "payload")

    def test_file_write_carries_binary_bytes_intact(self):
        # cli.remote_run sends the binary herdr-task zipapp (a PK zip header, non-UTF-8 bytes).
        m = self.fakes.add_machine("vm-b")
        payload = b"PK\x03\x04\x00\xfb\x01binary zipapp\x80\x81"
        r = subprocess.run([os.path.join(self.fakes.bin_dir, "hc"), "file", "write", "--on", "vm-b",
                            ".local/bin/herdr-task"], env=self.env, input=payload,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(m["home"], ".local/bin/herdr-task"), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_file_write_refuses_a_path_that_escapes_the_machine_home(self):
        m = self.fakes.add_machine("vm-b")
        outside = os.path.join(self.root, "escaped.txt")
        for target in ("/" + outside.lstrip("/"), "../../../../../../.." + outside):
            r = self.hc("file", "write", "--on", "vm-b", target, stdin="nope")
            self.assertEqual(r.returncode, 1, target)
            self.assertIn("refusing", r.stderr)
        self.assertFalse(os.path.exists(outside))
        # Nothing landed outside the machine home.
        self.assertTrue(m["home"].startswith(self.root))

    def test_exec_reaches_a_real_herdr_task_on_the_machine_store(self):
        """The AutomationFromAnotherMachine pattern: a command sent with `hc exec --on` runs against
        that machine's own store, not the home machine's."""
        m = self.fakes.add_machine("vm-b")
        probe = ("import os, json; print(json.dumps({'store': os.environ['HERDR_TASK_STORE']}))")
        r = self.hc("exec", "--on", "vm-b", "--", sys.executable, "-c", probe)
        self.assertEqual(json.loads(r.stdout)["store"], m["store"])

    def test_exec_keeps_the_real_home_but_runs_in_the_machine_dir(self):
        # base.guard refuses hc under a changed HOME, so a remote herdr-task needs the real HOME; a
        # remote command's relative paths are isolated by running in the machine's own directory.
        m = self.fakes.add_machine("vm-b")
        r = self.hc("exec", "--on", "vm-b", "--", "sh", "-c", "echo $HOME; pwd")
        home, cwd = r.stdout.strip().splitlines()
        self.assertEqual(os.path.realpath(home), os.path.realpath(os.environ["HOME"]))
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(m["home"]))


# ---- the call log and isolation ---------------------------------------------------------------

class CallLog(Base):
    def test_every_call_is_recorded_for_both_fakes(self):
        self.gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        self.hc("label", "addr", "k=v")
        gh_calls = self.fakes.calls("gh")
        hc_calls = self.fakes.calls("hc")
        self.assertEqual(gh_calls[-1]["argv"][:2], ["repo", "view"])
        self.assertEqual(hc_calls[-1]["argv"], ["label", "addr", "k=v"])
        self.assertEqual(len(self.fakes.calls()), 2)

    def test_reset_clears_the_log(self):
        self.hc("label", "a", "b")
        self.assertTrue(self.fakes.calls())
        self.fakes.reset_calls()
        self.assertEqual(self.fakes.calls(), [])


class Isolation(Base):
    def test_the_fakes_never_write_to_home(self):
        """With `HOME` pointed at an empty scratch directory, a full round of fake calls leaves it
        empty: the fakes keep all their state under `FLOWFAKES_STATE`, never under the home."""
        empty_home = os.path.join(self.root, "empty-home")
        os.makedirs(empty_home)
        env = self.fakes.env(home=empty_home)
        self.env = env  # self.gh / self.hc use self.env
        self.branch("task/HAT-14-x")
        self.gh("pr", "create", "--repo", "octo/demo", "--head", "task/HAT-14-x", "--base", "main",
                "--title", "T", "--body", "b")
        self.fakes.open_pr("task/HAT-14-x")
        self.gh("pr", "merge", "1", "--repo", "octo/demo", "--squash")  # the heaviest path (git clone, merge)
        self.hc("machine", "ls", "--json")
        self.assertEqual(os.listdir(empty_home), [], "the fakes wrote into the home directory")

    def test_the_state_and_repos_stay_inside_the_root(self):
        self.assertTrue(self.fakes.state_dir.startswith(self.root))
        self.assertTrue(os.path.isfile(os.path.join(self.fakes.state_dir, "state.json")))
        for p in (self.fakes.origin, self.fakes.home_repo, self.fakes.home):
            self.assertTrue(p.startswith(self.root), p)

    def test_the_fakes_run_not_any_real_hc_or_gh(self):
        """Resolving `hc` and `gh` on the harness PATH finds the fakes, so a real one is never run."""
        for tool in ("hc", "gh"):
            self.assertEqual(shutil.which(tool, path=self.env["PATH"]),
                             os.path.join(self.fakes.bin_dir, tool))


if __name__ == "__main__":
    unittest.main()
