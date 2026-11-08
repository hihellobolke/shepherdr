"""sync-branch (branchsync.py): the owned-paths precheck, a clean rebase, conflicts inside and
outside the owned paths, a lockfile conflict and an abort that restores the branch and keeps the
backup ref, in a real throwaway git repo and a bare repo as the remote. git and ready_guards (its
owned-paths check) are real; only hc and gh would be mocked, and neither is reached here."""

import io
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
sys.path.insert(0, BIN)
sys.dont_write_bytecode = True

from herdr_task import branchsync  # noqa: E402

# Isolate every git call (this test's and branchsync's own) from the user's global and system
# config, so hooks, signing or an fsmonitor there never slow the rebases or hold the temp dirs open.
# Kept for the lifetime of this module's tests and restored after, so nothing leaks into later test
# files in a full `unittest discover` run.
_ENV_PATCH = None
_GITCONFIG = None


def setUpModule():
    global _ENV_PATCH, _GITCONFIG
    _fd, _GITCONFIG = tempfile.mkstemp(prefix="herdr-gitconfig-")
    os.close(_fd)
    _ENV_PATCH = mock.patch.dict(os.environ, {
        "GIT_CONFIG_GLOBAL": _GITCONFIG,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    _ENV_PATCH.start()


def tearDownModule():
    if _ENV_PATCH is not None:
        _ENV_PATCH.stop()
    if _GITCONFIG and os.path.exists(_GITCONFIG):
        os.unlink(_GITCONFIG)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def init_repo(repo, bare=False):
    os.makedirs(repo, exist_ok=True)
    if bare:
        git(repo, "init", "-q", "--bare", "-b", "main")
        return
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "commit.gpgsign", "false")


def write(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w") as f:
        f.write(text)


def commit(repo, name, text, msg):
    write(repo, name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def head(repo, ref="HEAD"):
    return git(repo, "rev-parse", ref)


def on_branch(repo):
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def rebase_in_progress(repo):
    gd = git(repo, "rev-parse", "--git-dir")
    gd = gd if os.path.isabs(gd) else os.path.join(repo, gd)
    return os.path.exists(os.path.join(gd, "rebase-merge")) or os.path.exists(os.path.join(gd, "rebase-apply"))


class SplitConflicts(unittest.TestCase):
    def test_outside_and_regenerate(self):
        """A conflicted file outside the owned paths is reported outside; a lockfile is reported to
        regenerate; an owned file is neither."""
        conflicted = ["src/a.ts", "other/x.ts", "bun.lock", "web/generated/g.ts"]
        outside, regenerate = branchsync._split_conflicts(conflicted, ["src/a.ts"])
        self.assertEqual(outside, ["other/x.ts"])
        self.assertEqual(sorted(regenerate), ["bun.lock", "web/generated/g.ts"])


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _local_repo(self):
        """A repo on branch `task`, forked from `feature` after `feature` moved ahead."""
        repo = os.path.join(self.tmp, "repo")
        init_repo(repo)
        commit(repo, "base.txt", "0\n", "base")
        git(repo, "branch", "feature")
        git(repo, "checkout", "-q", "feature")
        git(repo, "checkout", "-q", "-b", "task")
        return repo

    def test_clean_rebase_local(self):
        repo = self._local_repo()
        commit(repo, "a.txt", "work\n", "task work")
        before = head(repo)
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "clean")
        self.assertTrue(os.path.exists(os.path.join(repo, "b.txt")))  # feature's commit is now in
        self.assertTrue(os.path.exists(os.path.join(repo, "a.txt")))  # the worker's work stayed
        self.assertNotEqual(head(repo), before)  # rebased on top of feature
        self.assertEqual(head(repo, res["backup_ref"]), before)  # the backup ref kept the old tip
        self.assertEqual(on_branch(repo), "task")

    def test_backup_ref_survives_rebase_update_refs(self):
        """With rebase.updateRefs on, git would move every branch pointing into the rebased range,
        including the backup branch, off the original tip. sync forces it off so the backup holds."""
        repo = self._local_repo()
        git(repo, "config", "rebase.updateRefs", "true")
        before = commit(repo, "a.txt", "work\n", "task work")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "clean")
        self.assertEqual(head(repo, res["backup_ref"]), before)  # backup kept the original tip

    def test_conflict_inside_owned_paths_restores_branch(self):
        repo = self._local_repo()
        before = commit(repo, "a.txt", "task side\n", "task work")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "a.txt", "feature side\n", "feature changes the same file")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "conflict")
        self.assertEqual(res["conflicted"], ["a.txt"])
        self.assertEqual(res["outside_owns"], [])  # a.txt is owned
        self.assertEqual(res["regenerate"], [])
        # The branch is restored and the backup ref is kept.
        self.assertEqual(head(repo), before)
        self.assertFalse(rebase_in_progress(repo))
        self.assertEqual(head(repo, res["backup_ref"]), before)
        self.assertFalse(os.path.exists(os.path.join(repo, "b.txt")))

    def test_conflict_lockfile_is_reported_to_regenerate(self):
        repo = self._local_repo()
        write(repo, "a.txt", "task side\n")
        write(repo, "bun.lock", "task-lock\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "task work plus lockfile")
        before = head(repo)
        git(repo, "checkout", "-q", "feature")
        write(repo, "a.txt", "feature side\n")
        write(repo, "bun.lock", "feature-lock\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feature changes both")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "conflict")
        self.assertIn("bun.lock", res["conflicted"])
        self.assertEqual(res["regenerate"], ["bun.lock"])  # never merged by hand (rule 7)
        self.assertEqual(res["outside_owns"], [])          # the lockfile is exempt, not outside
        self.assertEqual(head(repo), before)               # restored

    def test_conflict_outside_owned_paths_via_rename(self):
        """The precheck passes (the worker only touched its owned file), but the feature branch
        renamed that file to an unowned path and edited it, so the rebase conflicts outside the owned
        paths. The conflict is reported there and the branch is restored."""
        repo = self._local_repo()
        lines = "".join(f"line{i}\n" for i in range(1, 31))
        write(repo, "src/a.ts", lines)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "shared file")
        git(repo, "branch", "-f", "feature", "HEAD")
        # The worker edits one line of its owned file.
        write(repo, "src/a.ts", lines.replace("line15\n", "TASK-EDIT\n"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "task edits owned src/a.ts")
        before = head(repo)
        # The feature branch renames the file out of the owned paths and edits the same line.
        git(repo, "checkout", "-q", "feature")
        os.makedirs(os.path.join(repo, "other"), exist_ok=True)
        git(repo, "mv", "src/a.ts", "other/x.ts")
        write(repo, "other/x.ts", lines.replace("line15\n", "FEATURE-EDIT\n"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feature renames and edits")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["src/a.ts"], "local", checks=False)

        self.assertEqual(res["reason"], "conflict")
        self.assertEqual(res["conflicted"], ["other/x.ts"])
        self.assertEqual(res["outside_owns"], ["other/x.ts"])  # outside the owned paths
        self.assertEqual(res["regenerate"], [])
        self.assertEqual(head(repo), before)                   # branch restored
        self.assertFalse(rebase_in_progress(repo))
        self.assertEqual(head(repo, res["backup_ref"]), before)

    def test_conflict_still_reported_with_rerere_autoupdate(self):
        """With rerere.autoupdate on and a cached resolution, git would stage the replayed resolution
        and the conflict would vanish from the report. --no-rerere-autoupdate keeps it unmerged, so
        the conflict is still reported and the branch still restored."""
        repo = os.path.join(self.tmp, "rr")
        init_repo(repo)
        git(repo, "config", "rerere.enabled", "true")
        git(repo, "config", "rerere.autoupdate", "true")
        base_lines = "l1\nl2\nl3\n"
        commit(repo, "base.txt", "0\n", "base")
        write(repo, "a.txt", base_lines)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "shared a.txt")
        shared = head(repo)
        # feature side of the conflict.
        git(repo, "checkout", "-q", "-b", "feature")
        write(repo, "a.txt", base_lines.replace("l2", "FEATURE"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feature edits l2")
        # task makes the conflicting edit, then resolves it once so rerere records the resolution.
        git(repo, "checkout", "-q", "-b", "task", shared)
        write(repo, "a.txt", base_lines.replace("l2", "TASK"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "task edits l2")
        git(repo, "rebase", "feature", check=False)  # conflicts
        write(repo, "a.txt", base_lines.replace("l2", "RESOLVED"))
        git(repo, "add", "a.txt")
        git(repo, "rebase", "--continue", check=False)
        # Recreate the same conflict (same preimage) so rerere replays its cached resolution.
        git(repo, "checkout", "-q", "-B", "task", shared)
        write(repo, "a.txt", base_lines.replace("l2", "TASK"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "task edits l2 again")
        before = head(repo)

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "conflict")
        self.assertEqual(res["conflicted"], ["a.txt"])  # not erased by rerere autoupdate
        self.assertEqual(head(repo), before)            # branch restored
        self.assertFalse(rebase_in_progress(repo))

    def test_dirty_worktree_is_refused_without_losing_work(self):
        """A rebase will not start with uncommitted tracked changes, so sync-branch refuses up front
        and never runs a reset that could discard them."""
        repo = self._local_repo()
        before = commit(repo, "a.txt", "committed\n", "task work")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")
        write(repo, "a.txt", "uncommitted edit\n")  # dirty

        with self.assertRaises(branchsync.Fail):
            branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        with open(os.path.join(repo, "a.txt")) as f:
            self.assertEqual(f.read(), "uncommitted edit\n")  # the uncommitted work survived
        self.assertEqual(head(repo), before)                   # nothing rebased
        self.assertFalse(os.path.exists(os.path.join(repo, "b.txt")))

    def test_owned_paths_precheck_refuses_before_touching_the_branch(self):
        """A change outside the owned paths is refused with owned-paths; nothing is rebased and no
        backup ref is made (the 'outside owned paths' case, section 6.8)."""
        repo = self._local_repo()
        write(repo, "a.txt", "work\n")
        write(repo, "other.txt", "not mine\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "touches a file outside owns")
        before = head(repo)
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)

        self.assertEqual(res["reason"], "owned-paths")
        self.assertEqual(res["outside_owns"], ["other.txt"])
        self.assertEqual(res["backup_ref"], "")        # nothing created
        self.assertEqual(head(repo), before)           # nothing rebased
        self.assertFalse(os.path.exists(os.path.join(repo, "b.txt")))

    def test_checks_failed_after_clean_rebase(self):
        repo = self._local_repo()
        commit(repo, "a.txt", "work\n", "task work")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        os.environ["HERDR_TASK_CHECK"] = "false"
        try:
            res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=True)
        finally:
            del os.environ["HERDR_TASK_CHECK"]

        self.assertEqual(res["reason"], "checks-failed")
        self.assertTrue(res["backup_ref"])
        self.assertTrue(os.path.exists(os.path.join(repo, "b.txt")))  # the rebase did land

    def test_checks_pass_after_clean_rebase(self):
        repo = self._local_repo()
        commit(repo, "a.txt", "work\n", "task work")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        os.environ["HERDR_TASK_CHECK"] = "true"
        try:
            res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=True)
        finally:
            del os.environ["HERDR_TASK_CHECK"]
        self.assertEqual(res["reason"], "clean")

    def test_already_current_is_clean(self):
        repo = self._local_repo()
        commit(repo, "a.txt", "work\n", "task work")
        res = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)
        self.assertEqual(res["reason"], "clean")

    def test_owns_exempt_lets_an_extra_path_pass_the_precheck(self):
        """A changed file outside owns is refused, but adding it to exempt (the space's owns_exempt)
        lets the precheck pass (C6)."""
        repo = self._local_repo()
        write(repo, "a.txt", "work\n")
        write(repo, "config.gen", "generated\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "task work plus an extra file")
        git(repo, "checkout", "-q", "feature")
        commit(repo, "b.txt", "new\n", "feature moves")
        git(repo, "checkout", "-q", "task")

        refused = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False)
        self.assertEqual(refused["reason"], "owned-paths")
        self.assertEqual(refused["outside_owns"], ["config.gen"])

        # feature moved on only b.txt, so with config.gen exempt the rebase is clean.
        ok = branchsync.sync(repo, "feature", ["a.txt"], "local", checks=False, exempt=["config.gen"])
        self.assertEqual(ok["reason"], "clean")

    def test_clean_rebase_remote_bare_remote(self):
        """Remote mode fetches the feature branch from a bare repo, then rebases onto it."""
        origin = os.path.join(self.tmp, "origin.git")
        init_repo(origin, bare=True)
        a = os.path.join(self.tmp, "a")
        init_repo(a)
        commit(a, "base.txt", "0\n", "base")
        git(a, "remote", "add", "origin", origin)
        git(a, "push", "-q", "origin", "main")
        git(a, "checkout", "-q", "-b", "feature")
        git(a, "push", "-q", "origin", "feature")

        work = os.path.join(self.tmp, "work")
        git(self.tmp, "clone", "-q", origin, "work")
        git(work, "config", "user.email", "w@example.com")
        git(work, "config", "user.name", "Worker")
        git(work, "config", "commit.gpgsign", "false")
        git(work, "checkout", "-q", "-b", "task", "origin/feature")
        commit(work, "a.txt", "work\n", "task work")
        before = head(work)

        # feature moves ahead on the remote.
        git(a, "checkout", "-q", "feature")
        commit(a, "b.txt", "new\n", "feature moves")
        git(a, "push", "-q", "origin", "feature")

        res = branchsync.sync(work, "feature", ["a.txt"], "remote", checks=False)

        self.assertEqual(res["reason"], "clean")
        self.assertTrue(os.path.exists(os.path.join(work, "b.txt")))
        self.assertEqual(head(work, res["backup_ref"]), before)

    def test_remote_single_branch_clone_fetches_the_feature(self):
        """In a single-branch clone the default fetch mapping does not cover the feature branch, so a
        plain `git fetch origin feature` leaves origin/feature stale. The explicit destination refspec
        updates it, so the rebase lands on the real feature tip."""
        origin = os.path.join(self.tmp, "origin.git")
        init_repo(origin, bare=True)
        a = os.path.join(self.tmp, "a")
        init_repo(a)
        commit(a, "base.txt", "0\n", "base")
        git(a, "remote", "add", "origin", origin)
        git(a, "push", "-q", "origin", "main")
        git(a, "checkout", "-q", "-b", "feature")
        git(a, "push", "-q", "origin", "feature")

        work = os.path.join(self.tmp, "work")
        git(self.tmp, "clone", "-q", "--single-branch", "--branch", "main", origin, "work")
        git(work, "config", "user.email", "w@example.com")
        git(work, "config", "user.name", "Worker")
        git(work, "config", "commit.gpgsign", "false")
        git(work, "fetch", "-q", "origin", "+feature:refs/remotes/origin/feature")  # seed the task
        git(work, "checkout", "-q", "-b", "task", "origin/feature")
        commit(work, "a.txt", "work\n", "task work")

        # feature moves ahead on the remote; the single-branch mapping would not bring it in.
        git(a, "checkout", "-q", "feature")
        commit(a, "b.txt", "new\n", "feature moves")
        git(a, "push", "-q", "origin", "feature")

        res = branchsync.sync(work, "feature", ["a.txt"], "remote", checks=False)

        self.assertEqual(res["reason"], "clean")
        self.assertTrue(os.path.exists(os.path.join(work, "b.txt")))  # the real feature tip landed


class Helpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_backup_ref_points_at_head_and_enable_rerere(self):
        repo = os.path.join(self.tmp, "r")
        init_repo(repo)
        commit(repo, "x.txt", "1\n", "one")
        ref = branchsync.backup_ref(repo, "main")
        self.assertTrue(ref.startswith("backup/main-"))
        self.assertEqual(head(repo, ref), head(repo))
        branchsync.enable_rerere(repo)
        self.assertEqual(git(repo, "config", "rerere.enabled"), "true")

    def test_post_checks_env_command(self):
        repo = os.path.join(self.tmp, "r")
        init_repo(repo)
        commit(repo, "x.txt", "1\n", "one")
        os.environ["HERDR_TASK_CHECK"] = "true"
        try:
            self.assertEqual(branchsync.post_checks(repo, ["x.txt"]), {"ok": True, "detail": ""})
        finally:
            del os.environ["HERDR_TASK_CHECK"]
        os.environ["HERDR_TASK_CHECK"] = "false"
        try:
            self.assertFalse(branchsync.post_checks(repo, ["x.txt"])["ok"])
        finally:
            del os.environ["HERDR_TASK_CHECK"]

    def test_post_checks_no_command_is_ok_and_says_so(self):
        repo = os.path.join(self.tmp, "r")
        init_repo(repo)
        res = branchsync.post_checks(repo, [])
        self.assertTrue(res["ok"])
        self.assertEqual(res["detail"], "no project check command configured")

    def test_post_checks_uses_bin_check_script(self):
        repo = os.path.join(self.tmp, "r")
        init_repo(repo)
        os.makedirs(os.path.join(repo, "bin"))
        script = os.path.join(repo, "bin", "check")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(script, 0o755)
        self.assertFalse(branchsync.post_checks(repo, ["x.txt"])["ok"])


class Command(unittest.TestCase):
    """cmd_sync_branch glue: find this worktree's Task, print the 5-key envelope, map the reason to
    the exit code. sync() itself is covered above, so it is stubbed here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store_dir = os.path.join(self.tmp, "store")
        os.makedirs(self.store_dir)
        # Redirect the store and settings off the real ~/.herdr (captured at import).
        from herdr_task import store as _store, settings as _settings
        self._saved_store = _store.STORE
        self._saved_settings = _settings.SETTINGS
        _store.STORE = self.store_dir
        _settings.SETTINGS = os.path.join(self.tmp, "no-such-settings.json")
        self._term = os.environ.get("HC_TERMINAL_ID")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        from herdr_task import store as _store, settings as _settings
        _store.STORE = self._saved_store
        _settings.SETTINGS = self._saved_settings
        if self._term is None:
            os.environ.pop("HC_TERMINAL_ID", None)
        else:
            os.environ["HC_TERMINAL_ID"] = self._term

    def _store_with_task(self, feature=None):
        from herdr_task.store import Store, locate
        folder = os.path.join(self.tmp, "proj")
        init_repo(folder)
        commit(folder, "base.txt", "0\n", "base")
        with Store(locate(folder)) as s:
            t = s.new("Child", "do it")
            t.update(worktree=folder, owns=["a.txt"], base="feature", role="child",
                     agents=[{"address": "term1"}])
            if feature:
                t["feature"] = feature["id"]
                s.data["features"] = [feature]
            s.dirty = True
        os.environ["HC_TERMINAL_ID"] = "term1"
        return folder

    def _args(self, folder):
        return types.SimpleNamespace(folder=folder, base=None, json=True, id=None)

    def test_conflict_maps_to_code_3_and_prints_envelope(self):
        folder = self._store_with_task(feature={"id": 9, "mode": "local", "branch": "feature"})
        result = {"reason": "conflict", "outside_owns": ["x"], "conflicted": ["a.txt", "x"],
                  "regenerate": ["bun.lock"], "backup_ref": "backup/task-1", "detail": ""}
        seen = {}

        def fake_sync(wt, base, owns, mode, checks=True, exempt=None):
            seen.update(wt=wt, base=base, owns=owns, mode=mode, exempt=exempt)
            return result

        orig = branchsync.sync
        branchsync.sync = fake_sync
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = branchsync.cmd_sync_branch(self._args(folder))
        finally:
            branchsync.sync = orig

        self.assertEqual(code, 3)
        self.assertEqual(seen, {"wt": folder, "base": "feature", "owns": ["a.txt"], "mode": "local", "exempt": []})
        import json
        out = json.loads(buf.getvalue())
        self.assertEqual(set(out), {"reason", "conflicted", "outside_owns", "regenerate", "backup_ref"})
        self.assertEqual(out["reason"], "conflict")

    def test_clean_maps_to_code_0(self):
        folder = self._store_with_task(feature={"id": 9, "mode": "local", "branch": "feature"})
        orig = branchsync.sync
        branchsync.sync = lambda *a, **k: {"reason": "clean", "outside_owns": [], "conflicted": [],
                                           "regenerate": [], "backup_ref": "backup/task-1", "detail": ""}
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = branchsync.cmd_sync_branch(self._args(folder))
        finally:
            branchsync.sync = orig
        self.assertEqual(code, 0)

    def test_reads_owns_exempt_for_the_tasks_own_space(self):
        """cmd_sync_branch asks settings for the Task's own space and threads that space's owns_exempt
        into sync (not another space that shares the repo)."""
        from herdr_task.store import Store, locate
        folder = os.path.join(self.tmp, "proj")
        init_repo(folder)
        commit(folder, "base.txt", "0\n", "base")
        with Store(locate(folder)) as s:
            t = s.new("Child", "do it")
            t.update(worktree=folder, owns=["a.txt"], base="feature", role="child", space="sp-2",
                     feature=9, agents=[{"address": "term1"}])
            s.data["features"] = [{"id": 9, "mode": "local", "branch": "feature"}]
            s.dirty = True
        os.environ["HC_TERMINAL_ID"] = "term1"

        seen = {}
        orig_settings = branchsync.settings_for
        orig_sync = branchsync.sync
        branchsync.settings_for = lambda store, space=None: (
            seen.update(space=space) or {"tasks": {"owns_exempt": ["gen/**"]}})
        branchsync.sync = lambda *a, **k: (
            seen.update(exempt=k.get("exempt")) or {"reason": "clean", "outside_owns": [],
            "conflicted": [], "regenerate": [], "backup_ref": "b", "detail": ""})
        try:
            with redirect_stdout(io.StringIO()):
                code = branchsync.cmd_sync_branch(self._args(folder))
        finally:
            branchsync.settings_for = orig_settings
            branchsync.sync = orig_sync

        self.assertEqual(code, 0)
        self.assertEqual(seen["space"], "sp-2")          # the Task's own space (finding 2)
        self.assertEqual(seen["exempt"], ["gen/**"])     # that space's owns_exempt reaches sync


if __name__ == "__main__":
    unittest.main()
