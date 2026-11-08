"""The closing steps of a Feature (T16, proposal 6.11, contract C7).

These run against a real throwaway git repo (tempfile), the way the contract asks: only `gh` and the
archive tool (`openspec`) are faked, never git. The three scenarios the task calls for are a clean
close, a base conflict and a row with no merged child (Needs you); the rest pin each step and the
Integration Task.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
sys.dont_write_bytecode = True

from herdr_task import closing, github, store  # noqa: E402
from herdr_task.base import Fail  # noqa: E402

# A throwaway STORE off the real ~/.herdr, kept for this module's tests and restored after so it
# never leaks into later test files. Each test also patches store.STORE to its own tmp dir in setUp.
_ENV_PATCH = None


def setUpModule():
    global _ENV_PATCH
    _ENV_PATCH = mock.patch.dict(os.environ, {
        "HERDR_TASK_STORE": tempfile.mkdtemp(prefix="herdr-store-import-"),
    })
    _ENV_PATCH.start()


def tearDownModule():
    if _ENV_PATCH is not None:
        _ENV_PATCH.stop()


TABLE = """# Login flow

## Work table

| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 0.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 1.1 | Parse headers       | src/parser/header.ts | 0.1     | low  | S    |
| 1.2 | Parse body          | src/parser/body.ts   | 0.1     | high | M    |
| 2.1 | Wire parser         | src/parser/index.ts  | 1.1,1.2 | low  | M    |

## Tasks

- [ ] 0.1 Shared parser types
- [ ] 1.1 Parse headers
- [ ] 1.2 Parse body
- [ ] 2.1 Wire parser
"""

CHANGE = "add-login-flow"
BRANCH = "feature/login-flow"
ALL_ROWS = ["0.1", "1.1", "1.2", "2.1"]


def _script(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


FAKE_OPENSPEC = """#!/usr/bin/env python3
import datetime, os, shutil, sys
# argv: archive <change> -y ; cwd is the feature worktree. OpenSpec 1.3.1 archives to a dated dir:
# openspec/changes/archive/<YYYY-MM-DD>-<change>/ (dist/core/archive.js).
change = sys.argv[2]
src = os.path.join("openspec", "changes", change)
dst = os.path.join("openspec", "changes", "archive", f"{datetime.date.today().isoformat()}-{change}")
os.makedirs(os.path.dirname(dst), exist_ok=True)
shutil.move(src, dst)
"""

FAKE_GH = """#!/usr/bin/env python3
import sys
a = sys.argv[1:]
if a[:2] == ["repo", "view"]:
    print("acme/shop")
elif a[:2] == ["pr", "create"]:
    print("https://github.com/acme/shop/pull/7")
elif a[:2] == ["pr", "view"]:
    print("OPEN")
else:
    sys.exit(1)
"""

# An existing open pull request on the branch (at the pushed tip), and `pr create` fails ("already
# exists"): closing must adopt the existing one rather than try to create a second. The fake reads the
# pushed tip from the bare remote (HERDR_TEST_REMOTE) so its headRefOid matches.
FAKE_GH_EXISTING = """#!/usr/bin/env python3
import json, os, subprocess, sys
a = sys.argv[1:]
def tip():
    bare = os.environ.get("HERDR_TEST_REMOTE", "")
    return subprocess.run(["git", "-C", bare, "rev-parse", "feature/login-flow"],
                          capture_output=True, text=True).stdout.strip()
if a[:2] == ["repo", "view"]:
    print("acme/shop")
elif a[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 11, "url": "https://github.com/acme/shop/pull/11",
                       "baseRefName": "main", "isCrossRepository": False, "headRefOid": tip()}]))
elif a[:2] == ["pr", "create"]:
    sys.stderr.write("a pull request for branch already exists\\n"); sys.exit(1)
else:
    sys.exit(1)
"""

# `pr list` returns only a fork's PR (isCrossRepository) on the same branch name: closing must NOT
# adopt it, and must create its own (pull/7).
FAKE_GH_FORK = """#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
if a[:2] == ["repo", "view"]:
    print("acme/shop")
elif a[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 99, "url": "https://github.com/fork/shop/pull/99",
                       "baseRefName": "main", "isCrossRepository": True, "headRefOid": "deadbeef"}]))
elif a[:2] == ["pr", "create"]:
    print("https://github.com/acme/shop/pull/7")
else:
    sys.exit(1)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="herdr-closing-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            self.git("config", k, v)

        # A fake openspec on closing.OPENSPEC (the archive tool); git is never faked.
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        fake_openspec = os.path.join(self.bin, "openspec")
        _script(fake_openspec, FAKE_OPENSPEC)
        self._set(closing, "OPENSPEC", fake_openspec)

        # A throwaway STORE under this test's tmp.
        self.store_dir = os.path.join(self.tmp, "store")
        os.makedirs(self.store_dir)
        self._set(store, "STORE", self.store_dir)
        self.loc = store.locate(self.repo)

    def _set(self, mod, name, value):
        old = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, old)

    def _set_env(self, name, value):
        old = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, old) if old is not None
                        else os.environ.pop(name, None))

    # ---- git helpers -------------------------------------------------------------------------

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, content, cwd=None):
        path = os.path.join(cwd or self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def show(self, ref_path, branch=BRANCH):
        """A file's content on `branch` (for a committed, maybe moved, file)."""
        return subprocess.run(["git", "-C", self.repo, "show", f"{branch}:{ref_path}"],
                              capture_output=True, text=True).stdout

    def log_subjects(self, branch=BRANCH):
        out = self.git("log", "--format=%s", branch)
        return out.splitlines()

    def archived_rel(self, branch=BRANCH):
        """The committed path of the archived change on `branch` (the dated dir openspec uses)."""
        tree = self.git("ls-tree", "-r", "--name-only", branch)
        for line in tree.splitlines():
            if line.startswith("openspec/changes/archive/") and line.endswith(f"-{CHANGE}/tasks.md"):
                return line
        return None

    # ---- building the repo and the store -----------------------------------------------------

    def seed_main(self, extra=None):
        self.write("README.md", "repo\n")
        if extra:
            for name, content in extra.items():
                self.write(name, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")

    def make_feature_branch(self, table=TABLE, extra=None):
        self.git("checkout", "-q", "-b", BRANCH)
        self.write(f"openspec/changes/{CHANGE}/tasks.md", table)
        if extra:
            for name, content in extra.items():
                self.write(name, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "spec: work table")
        self.git("checkout", "-q", "main")

    def build_store(self, child_rows, mode="local", digests=None, states=None):
        """A Feature in `closing` with one child per row id in `child_rows`. `states` overrides a
        child's state (default: "done" = merged). `digests` maps a child's row id to a digest state
        for a remote child (machine vm-b, no local Task state)."""
        states = states or {}
        digests = digests or {}
        with store.Store(self.loc) as s:
            f = s.new_feature("Login flow", CHANGE, BRANCH, base="main", mode=mode)
            spec = s.new("Plan the login flow", "", "worktree", feature=f["id"], role="spec")
            f["spec_task"] = spec["id"]
            for rid in child_rows:
                remote_state = digests.get(rid)
                if remote_state is not None:
                    c = s.new(f"Child {rid}", "", "worktree", feature=f["id"], role="child",
                              rows=[rid], owns=[f"src/{rid}.ts"])
                    c["machine"] = "vm-b"
                    f["children"].append({"ref": c["id"], "machine": "vm-b", "package": rid,
                                          "rows": [rid], "role": "child"})
                    f["digests"][f"vm-b:{c['id']}"] = {"state": remote_state, "rows": [rid]}
                    continue
                c = s.new(f"Child {rid}", "", "worktree", feature=f["id"], role="child",
                          rows=[rid], owns=[f"src/{rid}.ts"])
                c["state"] = states.get(rid, "done")
                f["children"].append({"ref": c["id"], "machine": None, "package": rid,
                                      "rows": [rid], "role": "child"})
            f["phase"] = "closing"
            self.ref = s.ref(f)
        return self.ref


class CleanClose(Base):
    def test_a_clean_close_ticks_archives_and_opens_ready(self):
        self.seed_main()
        self.make_feature_branch()
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            feature = s.find_feature(ref)
            result = closing.close(s, feature)

        self.assertEqual(result["ticked"], ALL_ROWS)
        self.assertEqual(result["no_child"], [])
        self.assertTrue(result["archived"])
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["pr"])  # local mode: no pull request, the squash is the person's
        self.assertEqual(result["phase"], "ready")

        # The Feature moved to ready with no attention, and it persisted.
        with store.Store(self.loc, write=False) as s:
            f = s.find_feature(ref)
            self.assertEqual((f["phase"], f["attention"]), ("ready", ""))

        # Every box is ticked in the archived tasks.md on the feature branch (openspec archives it
        # under a dated directory, so find it rather than assume the name).
        archived = self.archived_rel()
        self.assertIsNotNone(archived, "the change was not archived under a dated dir")
        ticked = self.show(archived)
        for rid in ALL_ROWS:
            self.assertIn(f"- [x] {rid}", ticked)
        # The change folder was moved out of changes/ by the archive step.
        self.assertEqual(self.show(f"openspec/changes/{CHANGE}/tasks.md"), "")
        # One commit for the tick and one for the archive.
        subjects = self.log_subjects()
        self.assertTrue(any("tick merged rows" in s for s in subjects), subjects)
        self.assertTrue(any(s == f"Archive {CHANGE}" for s in subjects), subjects)

    def test_a_remote_child_is_merged_from_its_digest(self):
        """A child on another machine has no local Task state; its digest carries the outcome."""
        self.seed_main()
        self.make_feature_branch()
        # 0.1 and 1.1 local-merged, 1.2 and 2.1 remote-merged via their digests.
        ref = self.build_store(["0.1", "1.1"] + ["1.2", "2.1"],
                               digests={"1.2": "merged", "2.1": "merged"})
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertEqual(result["no_child"], [])
        self.assertEqual(result["ticked"], ALL_ROWS)


class RemoteClose(Base):
    def test_remote_mode_pushes_and_opens_a_pull_request(self):
        self.seed_main()
        # A bare remote to push the feature branch to.
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")
        self.make_feature_branch()

        fake_gh = os.path.join(self.bin, "gh")
        _script(fake_gh, FAKE_GH)
        self._set(github, "GH", fake_gh)

        ref = self.build_store(ALL_ROWS, mode="remote")
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertEqual(result["phase"], "ready")
        self.assertEqual(result["pr"], "https://github.com/acme/shop/pull/7")
        with store.Store(self.loc, write=False) as s:
            self.assertEqual(s.find_feature(ref)["pr"]["number"], 7)
        # The feature branch was pushed to the remote.
        self.assertTrue(subprocess.run(
            ["git", "-C", bare, "rev-parse", "--verify", BRANCH],
            capture_output=True).returncode == 0)

    def test_remote_mode_adopts_an_existing_pull_request(self):
        """A pull request already open on the branch (not saved on the record) is adopted, not
        re-created, so a retry reaches ready (reviewer major)."""
        self.seed_main()
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")
        self.make_feature_branch()

        self._set_env("HERDR_TEST_REMOTE", bare)
        fake_gh = os.path.join(self.bin, "gh")
        _script(fake_gh, FAKE_GH_EXISTING)  # `pr create` here fails; only adoption reaches ready
        self._set(github, "GH", fake_gh)

        ref = self.build_store(ALL_ROWS, mode="remote")
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertEqual(result["phase"], "ready")
        self.assertEqual(result["pr"], "https://github.com/acme/shop/pull/11")
        with store.Store(self.loc, write=False) as s:
            self.assertEqual(s.find_feature(ref)["pr"]["number"], 11)

    def test_remote_mode_never_adopts_a_fork_pull_request(self):
        """A fork's PR on an identically named branch is not adopted; closing creates its own."""
        self.seed_main()
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")
        self.make_feature_branch()

        fake_gh = os.path.join(self.bin, "gh")
        _script(fake_gh, FAKE_GH_FORK)
        self._set(github, "GH", fake_gh)

        ref = self.build_store(ALL_ROWS, mode="remote")
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertEqual(result["pr"], "https://github.com/acme/shop/pull/7")  # created, not the fork
        with store.Store(self.loc, write=False) as s:
            self.assertEqual(s.find_feature(ref)["pr"]["number"], 7)


class BaseConflict(Base):
    def test_a_base_conflict_aborts_and_makes_an_integration_task(self):
        self.seed_main(extra={"src/shared.ts": "base\n"})
        # The feature branch and the base both change the same file after the branch point.
        self.make_feature_branch(extra={"src/shared.ts": "feature\n"})
        self.write("src/shared.ts", "main\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base moves shared.ts")

        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            feature = s.find_feature(ref)
            result = closing.close(s, feature)

        self.assertTrue(result["conflict"])
        self.assertIn("src/shared.ts", result["conflicted"])
        self.assertTrue(result["archived"])  # the archive commit happened before the merge
        self.assertIsNotNone(result["integration_task"])

        with store.Store(self.loc, write=False) as s:
            f = s.find_feature(ref)
            self.assertEqual(f["attention"], "base-conflict")
            self.assertNotEqual(f["phase"], "ready")  # still closing, Needs you
            integ = s.find(result["integration_task"])
            self.assertEqual(integ["role"], "integration")
            self.assertEqual(integ["risk"], "high")
            self.assertEqual(integ["base"], BRANCH)
            self.assertEqual((integ.get("set") or {}).get("merge"), "ask")
            self.assertEqual(integ["state"], "queued")
            self.assertTrue(any(c.get("role") == "integration" for c in f["children"]))

        # The merge was aborted: the feature branch has no conflict markers left.
        self.assertNotIn("<<<<<<<", self.show("src/shared.ts"))

    def test_close_is_idempotent_while_an_integration_task_is_open(self):
        """A second close while the Integration Task is still open makes no duplicate."""
        self.seed_main(extra={"src/shared.ts": "base\n"})
        self.make_feature_branch(extra={"src/shared.ts": "feature\n"})
        self.write("src/shared.ts", "main\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base moves shared.ts")
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            first = closing.close(s, s.find_feature(ref))
        with store.Store(self.loc) as s:
            f = s.find_feature(ref)
            second = closing.close(s, f)
            self.assertEqual(second["integration_task"], first["integration_task"])
            self.assertEqual(sum(1 for c in f["children"] if c.get("role") == "integration"), 1)


class Resume(Base):
    def _archive_on_branch(self):
        """Mimic a prior close that ticked and archived the change on the feature branch, before it
        hit a base conflict that has since been resolved. Uses the real dated archive layout."""
        import datetime
        self.git("checkout", "-q", BRANCH)
        src = os.path.join(self.repo, "openspec", "changes", CHANGE)
        dated = f"{datetime.date.today().isoformat()}-{CHANGE}"
        dst = os.path.join(self.repo, "openspec", "changes", "archive", dated)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "archived earlier")
        self.git("checkout", "-q", "main")

    def test_close_resumes_after_the_archive_step(self):
        """Re-entering closing when the change is already archived skips tick and archive, merges the
        base and reaches ready, instead of failing to find the moved tasks.md (reviewer blocker)."""
        self.seed_main()
        self.make_feature_branch()
        self._archive_on_branch()
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertEqual(result["ticked"], [])      # nothing to tick on a resume
        self.assertTrue(result["archived"])         # recognised as already archived
        self.assertFalse(result["conflict"])
        self.assertEqual(result["phase"], "ready")

    def test_close_refuses_a_dirty_feature_worktree(self):
        """A worktree already checked out on the feature branch with uncommitted work is never
        committed into a closing commit (reviewer blocker)."""
        self.seed_main()
        self.make_feature_branch()
        holder = tempfile.mkdtemp(prefix="feat-wt-")
        self.addCleanup(shutil.rmtree, holder, True)
        wt = os.path.join(holder, "wt")
        self.git("worktree", "add", "--quiet", wt, BRANCH)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", wt))
        self.write("junk.txt", "unrelated work\n", cwd=wt)  # untracked, uncommitted
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            with self.assertRaises(Fail):
                closing.close(s, s.find_feature(ref))


class RowNoChild(Base):
    def test_a_row_with_no_merged_child_shows_needs_you(self):
        self.seed_main()
        self.make_feature_branch()
        # Row 2.1's child never merged (still running); the other three merged.
        ref = self.build_store(ALL_ROWS, states={"2.1": "running"})
        with store.Store(self.loc) as s:
            feature = s.find_feature(ref)
            result = closing.close(s, feature)

        self.assertEqual(result["no_child"], ["2.1"])
        self.assertEqual(result["ticked"], ["0.1", "1.1", "1.2"])
        self.assertFalse(result["archived"])  # closing stops before the archive step

        with store.Store(self.loc, write=False) as s:
            f = s.find_feature(ref)
            self.assertEqual(f["attention"], "row-no-child")
            self.assertNotEqual(f["phase"], "ready")

        # The three merged rows were still ticked and committed on the feature branch (not archived).
        ticked = self.show(f"openspec/changes/{CHANGE}/tasks.md")
        for rid in ("0.1", "1.1", "1.2"):
            self.assertIn(f"- [x] {rid}", ticked)
        self.assertIn("- [ ] 2.1", ticked)

    def test_a_skipped_row_counts_as_no_merged_child(self):
        self.seed_main()
        self.make_feature_branch()
        ref = self.build_store(ALL_ROWS, states={"1.2": "skipped"})
        # mark that child skipped (states only sets state; skipped is its own flag)
        with store.Store(self.loc) as s:
            f = s.find_feature(ref)
            child = next(s.find(f"{s.prefix}-{c['ref']}") for c in f["children"]
                         if c["rows"] == ["1.2"])
            child["skipped"] = True
            s.dirty = True
        with store.Store(self.loc) as s:
            result = closing.close(s, s.find_feature(ref))
        self.assertIn("1.2", result["no_child"])
        self.assertNotIn("1.2", result["ticked"])


class Steps(Base):
    """Each public step on its own."""

    def test_tick_merged_rows_returns_rows_with_no_merged_child(self):
        self.seed_main()
        self.make_feature_branch()
        ref = self.build_store(["0.1", "1.1"])  # only two of the four rows have a child
        with store.Store(self.loc) as s:
            no_child = closing.tick_merged_rows(s, s.find_feature(ref))
        self.assertEqual(no_child, ["1.2", "2.1"])
        ticked = self.show(f"openspec/changes/{CHANGE}/tasks.md")
        self.assertIn("- [x] 0.1", ticked)
        self.assertIn("- [x] 1.1", ticked)
        self.assertIn("- [ ] 1.2", ticked)

    def test_tick_makes_no_commit_when_nothing_merged(self):
        self.seed_main()
        self.make_feature_branch()
        before = self.git("rev-parse", BRANCH)
        ref = self.build_store(["0.1"], states={"0.1": "running"})
        with store.Store(self.loc) as s:
            no_child = closing.tick_merged_rows(s, s.find_feature(ref))
        self.assertEqual(no_child, ALL_ROWS)
        self.assertEqual(self.git("rev-parse", BRANCH), before)  # no empty commit

    def test_merge_base_in_is_clean_when_the_base_is_an_ancestor(self):
        self.seed_main()
        self.make_feature_branch()
        self.build_store(ALL_ROWS)
        wt_holder = tempfile.mkdtemp(prefix="wt-")
        self.addCleanup(shutil.rmtree, wt_holder, True)
        wt = os.path.join(wt_holder, "wt")
        self.git("worktree", "add", "--quiet", wt, BRANCH)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", wt))
        r = closing.merge_base_in(wt, {"base": "main", "branch": BRANCH, "mode": "local"})
        self.assertFalse(r["conflict"])

    def test_archive_change_stages_only_openspec(self):
        """An unrelated untracked file in the worktree is never swept into the archive commit."""
        self.seed_main()
        self.make_feature_branch()
        wt_holder = tempfile.mkdtemp(prefix="wt-")
        self.addCleanup(shutil.rmtree, wt_holder, True)
        wt = os.path.join(wt_holder, "wt")
        self.git("worktree", "add", "--quiet", wt, BRANCH)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", wt))
        self.write("junk.txt", "unrelated\n", cwd=wt)
        closing.archive_change(wt, CHANGE)
        self.assertEqual(self.show("junk.txt"), "")  # not committed to the branch
        self.assertEqual(self.git("log", "-1", "--format=%s", BRANCH), f"Archive {CHANGE}")

    def test_merge_base_in_raises_when_a_remote_fetch_fails(self):
        """Remote mode with no reachable remote stops instead of merging a stale local base."""
        self.seed_main()
        self.make_feature_branch()
        wt_holder = tempfile.mkdtemp(prefix="wt-")
        self.addCleanup(shutil.rmtree, wt_holder, True)
        wt = os.path.join(wt_holder, "wt")
        self.git("worktree", "add", "--quiet", wt, BRANCH)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", wt))
        with self.assertRaises(Fail):
            closing.merge_base_in(wt, {"base": "main", "branch": BRANCH, "mode": "remote"})

    def test_archive_change_moves_and_commits(self):
        self.seed_main()
        self.make_feature_branch()
        wt_holder = tempfile.mkdtemp(prefix="wt-")
        self.addCleanup(shutil.rmtree, wt_holder, True)
        wt = os.path.join(wt_holder, "wt")
        self.git("worktree", "add", "--quiet", wt, BRANCH)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", wt))
        import datetime
        closing.archive_change(wt, CHANGE)
        dated = f"{datetime.date.today().isoformat()}-{CHANGE}"
        self.assertTrue(os.path.isdir(os.path.join(wt, "openspec", "changes", "archive", dated)))
        self.assertFalse(os.path.isdir(os.path.join(wt, "openspec", "changes", CHANGE)))
        self.assertEqual(self.git("log", "-1", "--format=%s", BRANCH), f"Archive {CHANGE}")

    def test_open_pr_local_mode_moves_to_ready_with_no_pr(self):
        self.seed_main()
        self.make_feature_branch()
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            f = s.find_feature(ref)
            out = closing.open_pr(s, f)
            self.assertIsNone(out["pr"])
            self.assertEqual(f["phase"], "ready")

    def test_make_integration_task_is_created_once(self):
        self.seed_main()
        self.make_feature_branch()
        ref = self.build_store(ALL_ROWS)
        with store.Store(self.loc) as s:
            f = s.find_feature(ref)
            t1 = closing.make_integration_task(s, f)
            t2 = closing.make_integration_task(s, f)
            self.assertEqual(t1["id"], t2["id"])
            self.assertEqual(t1["base"], BRANCH)
            self.assertEqual(t1["role"], "integration")
            self.assertEqual(t1["risk"], "high")


if __name__ == "__main__":
    unittest.main()
