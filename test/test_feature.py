"""The Feature lifecycle and the `feature` command (feature.py, proposal T11, contract C7).

A Feature is a record of its own (ADR-0001). These tests cover `feature add` (the record, the
feature branch and the spec Task), the mode check (remote or local) with its warning, `show`,
`sync`, `cancel`, `merge`, `fix-round`, and the Plan approval hook. They prove each phase change,
the local-mode guard (no GitHub remote), and that cancel deletes nothing.

A real throwaway git repo (tempfile) and a throwaway STORE are used; no real HOME, HERDR_APP_HOME
or ~/.herdr is touched. `phases.py` is built by a parallel task (T11a) and is not on this branch,
so a small fake is injected as `herdr_task.phases` (feature.py imports it lazily). hc and gh are the
only programs mocked; git is real, and the few places that would reach hc are monkeypatched so no
real core is touched."""

import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
sys.dont_write_bytecode = True

# A throwaway STORE and settings file, set before the package is imported, so the real ones are
# never read or written.
os.environ.setdefault("HERDR_TASK_STORE", tempfile.mkdtemp(prefix="herdr-feature-store-import-"))
os.environ.setdefault("HERDR_TASK_SETTINGS", os.path.join(
    tempfile.mkdtemp(prefix="herdr-feature-settings-"), "shepherdr.json"))

# Isolate every git call from the user's global and system config (hooks, signing, fsmonitor).
_EMPTY_GITCONFIG = tempfile.NamedTemporaryFile(prefix="herdr-feature-gitconfig-", delete=False).name
os.environ["GIT_CONFIG_GLOBAL"] = _EMPTY_GITCONFIG
os.environ["GIT_CONFIG_SYSTEM"] = os.devnull

# The fake phases module (T11a's PHASES are the one list of phase names, C7). feature.py imports it
# lazily inside _set_phase, so injecting it here is enough.
PHASES = ("planning", "building", "verifying", "closing", "ready", "done", "cancelled")
_fake_phases = types.ModuleType("herdr_task.phases")
_fake_phases.PHASES = PHASES
_fake_phases.ATTENTION = ("verify-open", "row-no-child", "child-failed", "child-stalled",
                          "needs-plan", "needs-owns", "base-conflict", "blocker-finding")
_fake_phases.fix_round_allowed = lambda feature, verify_rounds: int(feature.get("fix_rounds") or 0) < verify_rounds
sys.modules.setdefault("herdr_task.phases", _fake_phases)

from herdr_task import feature  # noqa: E402
from herdr_task import store as store_mod  # noqa: E402
from herdr_task.store import Store, locate  # noqa: E402
from herdr_task.base import Fail  # noqa: E402


def git(repo, *args, check=True):
    import subprocess
    r = subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def init_repo(repo):
    os.makedirs(repo, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "commit.gpgsign", "false")


def commit(repo, name, text, msg):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w") as f:
        f.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def branch_exists(repo, name):
    import subprocess
    return subprocess.run(["git", "-C", repo, "show-ref", "--verify", "--quiet",
                           f"refs/heads/{name}"]).returncode == 0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="herdr-feature-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._saved_store = store_mod.STORE
        store_mod.STORE = os.path.join(self.tmp, "store")
        os.makedirs(store_mod.STORE, exist_ok=True)
        self.addCleanup(lambda: setattr(store_mod, "STORE", self._saved_store))
        self.repo = os.path.join(self.tmp, "repo")
        init_repo(self.repo)
        commit(self.repo, "README.md", "repo\n", "base")
        # Keep every hc/scheduler path out of the tests.
        self._patch(feature, "ensure_scheduler", lambda quiet=False: None)
        self._patch(feature, "live_agents", lambda key: [])
        self._patch(feature, "worker_argv", lambda s, t, space=None, agent=None: ["claude"])

    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, old))

    def store(self, write=True):
        return Store(locate(self.repo), write)


# ---- names and mode -------------------------------------------------------------------------

class Names(Base):
    def test_slug_title(self):
        self.assertEqual(feature.slug_title("Login flow"), "login-flow")
        self.assertEqual(feature.slug_title("  Weird!!  Title  "), "weird-title")
        self.assertEqual(feature.slug_title(""), "feature")

    def test_change_name_prefixes_add(self):
        self.assertEqual(feature.change_name("Login flow"), "add-login-flow")

    def test_change_name_keeps_a_verb(self):
        self.assertEqual(feature.change_name("Update the parser"), "update-the-parser")


class Mode(Base):
    def test_local_when_no_remote(self):
        self.assertEqual(feature.detect_mode(self.repo), "local")

    def test_local_when_not_a_repo(self):
        self.assertEqual(feature.detect_mode(os.path.join(self.tmp, "nope")), "local")

    def test_remote_when_github_and_gh_access(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/shop.git")
        self._patch(feature, "gh", lambda *a, **k: (True, "", ""))
        self.assertEqual(feature.detect_mode(self.repo), "remote")

    def test_local_when_github_remote_but_no_gh_access(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/shop.git")
        self._patch(feature, "gh", lambda *a, **k: (False, "", "not logged in"))
        self.assertEqual(feature.detect_mode(self.repo), "local")

    def test_local_when_remote_is_not_github(self):
        git(self.repo, "remote", "add", "origin", "https://gitlab.com/acme/shop.git")
        self.assertEqual(feature.detect_mode(self.repo), "local")

    def test_local_when_url_merely_contains_github(self):
        # A GitLab repo named github-mirror is not GitHub: the host must be github.com.
        git(self.repo, "remote", "add", "origin", "https://gitlab.com/acme/github-mirror.git")
        self._patch(feature, "gh", lambda *a, **k: (True, "", ""))
        self.assertEqual(feature.detect_mode(self.repo), "local")

    def test_remote_from_an_ssh_url(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
        self._patch(feature, "gh", lambda *a, **k: (True, "", ""))
        self.assertEqual(feature.detect_mode(self.repo), "remote")

    def test_remote_when_github_is_a_second_remote(self):
        git(self.repo, "remote", "add", "origin", "https://gitlab.com/acme/shop.git")
        git(self.repo, "remote", "add", "upstream", "https://github.com/acme/shop.git")
        self._patch(feature, "gh", lambda *a, **k: (True, "", ""))
        self.assertEqual(feature.detect_mode(self.repo), "remote")


# ---- create ---------------------------------------------------------------------------------

class Create(Base):
    def test_creates_record_branch_and_spec(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            prefix = s.prefix
            fb = f["branch"]
            spec = s.task_by_id(f["spec_task"])
            self.assertEqual(f["phase"], "planning")
            self.assertEqual(f["change"], "add-login-flow")
            self.assertEqual(f["mode"], "local")
            self.assertEqual(f["base"], "main")
            self.assertEqual(fb, f"feature/{prefix}-{f['id']}-login-flow")
            self.assertEqual(spec["role"], "spec")
            self.assertEqual(spec["feature"], f["id"])
            self.assertEqual(spec["base"], fb)
            self.assertTrue(spec["base_set"])
            self.assertEqual(spec["owns"], [])
            self.assertTrue(spec["branch"].startswith(f"task/{prefix}-{f['id']}-{prefix}-{spec['id']}-"))
        # The feature branch really exists in git, branched from main.
        self.assertTrue(branch_exists(self.repo, fb))
        self.assertEqual(git(self.repo, "rev-parse", fb), git(self.repo, "rev-parse", "main"))

    def test_feature_and_spec_share_the_counter(self):
        """A ref is a Feature or a Task, never both (ADR-0002): consecutive numbers from one counter."""
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            self.assertEqual(f["spec_task"], f["id"] + 1)
            with self.assertRaises(Fail):
                s.find(s.ref(f))  # the Feature's ref is not a Task

    def test_override_change_and_base(self):
        git(self.repo, "branch", "dev", "main")
        with self.store() as s:
            f = feature.create(s, "Login flow", base="dev", change="my-change", mode="local")
            self.assertEqual(f["change"], "my-change")
            self.assertEqual(f["base"], "dev")

    def test_no_auto_fanout(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local", auto_fanout=False)
            self.assertFalse(f["auto_fanout"])

    def test_refuses_duplicate_branch(self):
        # The branch name create() will generate already exists: a defensive refusal.
        with self.store(write=False) as s:
            taken = f"feature/{s.prefix}-{s.data['next']}-login-flow"
        git(self.repo, "branch", taken, "main")
        with self.store() as s:
            with self.assertRaises(Fail):
                feature.create(s, "Login flow", mode="local")


# ---- the Plan approval hook and the fan-out trigger read ------------------------------------

class SpecAccepted(Base):
    def _feature(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            return f["id"], f["spec_task"]

    def test_moves_planning_to_building(self):
        fid, spec_id = self._feature()
        with self.store() as s:
            feature.on_spec_accepted(s, s.task_by_id(spec_id))
        with self.store(write=False) as s:
            f = s.feature_by_id(fid)
            self.assertEqual(f["phase"], "building")
            self.assertIn("plan_approved_at", f)

    def test_idempotent_after_building(self):
        fid, spec_id = self._feature()
        with self.store() as s:
            feature.on_spec_accepted(s, s.task_by_id(spec_id))
            s.feature_by_id(fid)["phase"] = "verifying"
        with self.store() as s:
            feature.on_spec_accepted(s, s.task_by_id(spec_id))  # no move back
        with self.store(write=False) as s:
            self.assertEqual(s.feature_by_id(fid)["phase"], "verifying")

    def test_ignores_a_non_spec_task(self):
        fid, _ = self._feature()
        with self.store() as s:
            t = s.new("A child", "", "worktree", feature=fid, role="child")
            feature.on_spec_accepted(s, t)
        with self.store(write=False) as s:
            self.assertEqual(s.feature_by_id(fid)["phase"], "planning")

    def test_spec_merged(self):
        fid, spec_id = self._feature()
        with self.store(write=False) as s:
            f = s.feature_by_id(fid)
            self.assertFalse(feature.spec_merged(s, f))  # spec not done yet
        # The spec Task merges: its work table lands on the feature branch.
        with self.store() as s:
            s.task_by_id(spec_id)["state"] = "done"
            s.dirty = True
            f = s.feature_by_id(fid)
        git(self.repo, "worktree", "add", "-q", os.path.join(self.tmp, "wt"), f["branch"])
        wt = os.path.join(self.tmp, "wt")
        commit(wt, f"openspec/changes/{f['change']}/tasks.md", "| id |\n", "work table")
        git(self.repo, "worktree", "remove", "--force", wt)
        with self.store(write=False) as s:
            self.assertTrue(feature.spec_merged(s, s.feature_by_id(fid)))


# ---- show -----------------------------------------------------------------------------------

class Show(Base):
    def test_children_and_findings(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            child = s.new("Parse headers", "", "worktree", feature=f["id"], role="child",
                          owns=["src/a.ts"], risk="low", rows=["1.1"])
            child["state"] = "done"
            verify = s.new("Verify", "", "worktree", feature=f["id"], role="verify",
                           findings=[{"level": "major", "title": "x", "detail": "", "paths": [], "row": None}])
            f["children"] = [
                {"ref": child["id"], "machine": None, "package": "1.1", "rows": ["1.1"], "role": "child"},
                {"ref": 999, "machine": "vm-b", "package": "2.1", "rows": ["2.1"], "role": "child"},
            ]
            f["verify_task"] = verify["id"]
            f["digests"] = {"vm-b:999": {"state": "merged"}}
            data = feature.show(s, f)
            self.assertEqual(data["ref"], s.ref(f))
            local, remote = data["children"]
            self.assertEqual((local["ref"], local["state"], local["role"], local["machine"]),
                             (f"{s.prefix}-{child['id']}", "done", "child", None))
            self.assertEqual((remote["state"], remote["machine"], remote["digest"]["state"]),
                             ("merged", "vm-b", "merged"))
            self.assertEqual(len(data["findings"]), 1)


# ---- sync -----------------------------------------------------------------------------------

class Sync(Base):
    def _feature_branch(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            return f

    def test_clean_when_base_has_nothing_new(self):
        f = self._feature_branch()
        with self.store() as s:
            res = feature.sync(s, s.feature_by_id(f["id"]))
        self.assertEqual(res, {"ok": True, "merged": False, "conflict": False})

    def test_merges_when_base_moves_ahead(self):
        f = self._feature_branch()
        before = git(self.repo, "rev-parse", f["branch"])
        commit(self.repo, "new.txt", "new\n", "base moves")  # on main (checked out)
        with self.store() as s:
            res = feature.sync(s, s.feature_by_id(f["id"]))
        self.assertTrue(res["merged"])
        self.assertFalse(res["conflict"])
        self.assertNotEqual(git(self.repo, "rev-parse", f["branch"]), before)
        # The base commit is now reachable from the feature branch; the folder stayed on main.
        import subprocess
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "merge-base", "--is-ancestor",
                                         "main", f["branch"]]).returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_remote_sync_merges_the_fetched_base(self):
        """Remote mode fetches and merges the base at FETCH_HEAD, even without a tracking ref, so a
        feature branch behind the remote's base catches up."""
        import subprocess
        origin = os.path.join(self.tmp, "origin.git")
        os.makedirs(origin)
        git(origin, "init", "-q", "--bare", "-b", "main")
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="remote")
            fid, fb = f["id"], f["branch"]
        git(self.repo, "remote", "add", "origin", origin)
        git(self.repo, "push", "-q", "origin", "main", fb)
        before = git(self.repo, "rev-parse", fb)
        # Another clone advances origin's main past the feature branch.
        clone = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "clone", "-q", origin, clone], check=True,
                       env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "Tester")
        commit(clone, "remote-new.txt", "x\n", "remote advance")
        git(clone, "push", "-q", "origin", "main")

        with self.store() as s:
            res = feature.sync(s, s.feature_by_id(fid))
        self.assertTrue(res["merged"])
        self.assertFalse(res["conflict"])
        self.assertNotEqual(git(self.repo, "rev-parse", fb), before)

    def test_conflict_leaves_the_branch_untouched(self):
        f = self._feature_branch()
        # Both branches change the same file differently.
        wt = os.path.join(self.tmp, "fwt")
        git(self.repo, "worktree", "add", "-q", wt, f["branch"])
        commit(wt, "clash.txt", "feature side\n", "feature change")
        git(self.repo, "worktree", "remove", "--force", wt)
        commit(self.repo, "clash.txt", "base side\n", "base change")
        before = git(self.repo, "rev-parse", f["branch"])
        with self.store() as s:
            res = feature.sync(s, s.feature_by_id(f["id"]))
        self.assertEqual((res["merged"], res["conflict"]), (False, True))
        self.assertEqual(git(self.repo, "rev-parse", f["branch"]), before)  # branch untouched


# ---- cancel ---------------------------------------------------------------------------------

class Cancel(Base):
    def test_stops_running_removes_queued_and_deletes_nothing(self):
        killed = []
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            spec = s.task_by_id(f["spec_task"])  # queued by nothing here; stays todo -> removed
            running = s.new("Running child", "", "worktree", feature=f["id"], role="child")
            running["state"] = "running"
            running["branch"] = "task/child-running"
            running["worktree"] = os.path.join(self.tmp, "child-wt")
            queued = s.new("Queued child", "", "worktree", feature=f["id"], role="child")
            queued["state"] = "queued"
            queued["queued_at"] = 1
            running_key = s.key(running)
            fb = f["branch"]
            run_ref, spec_ref, queued_ref = s.ref(running), s.ref(spec), s.ref(queued)
        # A real child branch and a real git worktree, to prove cancel keeps both (deletes nothing).
        git(self.repo, "branch", "task/child-running", "main")
        git(self.repo, "worktree", "add", "-q", os.path.join(self.tmp, "child-wt"), "task/child-running")
        with open(os.path.join(self.tmp, "child-wt", "work.txt"), "w") as fh:
            fh.write("work\n")
        # Only the running child has a live agent; killing it goes through feature.run (hc kill).
        self._patch(feature, "live_agents", lambda key: [{"address": "hc://a1"}] if key == running_key else [])
        self._patch(feature, "run", lambda args: killed.append(args))

        with self.store() as s:
            res = feature.cancel(s, s.feature_by_id(f["id"]))

        self.assertEqual(res["stopped"], [run_ref])
        self.assertEqual(sorted(res["removed"]), sorted([spec_ref, queued_ref]))
        self.assertEqual(killed, [["hc", "kill", "hc://a1"]])
        with self.store(write=False) as s:
            self.assertEqual(s.feature_by_id(f["id"])["phase"], "cancelled")
            for ref in (run_ref, spec_ref, queued_ref):
                self.assertEqual(s.find(ref)["state"], "cancelled")
        # Deletes nothing: the feature branch, the real child branch and its real worktree all stay.
        self.assertTrue(branch_exists(self.repo, fb))
        self.assertTrue(branch_exists(self.repo, "task/child-running"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "child-wt", "work.txt")))
        self.assertIn(os.path.join(self.tmp, "child-wt"), git(self.repo, "worktree", "list"))


# ---- merge ----------------------------------------------------------------------------------

class Merge(Base):
    def _ready_feature(self, mode="local"):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode=mode)
            f["phase"] = "ready"
            fid = f["id"]
            fb = f["branch"]
        # A commit on the feature branch, beyond the base.
        wt = os.path.join(self.tmp, "merge-wt")
        git(self.repo, "worktree", "add", "-q", wt, fb)
        commit(wt, "feat.txt", "feature work\n", "feature work")
        git(self.repo, "worktree", "remove", "--force", wt)
        return fid, fb

    def test_refuses_when_not_ready(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            with self.assertRaises(Fail):
                feature.merge(s, s.feature_by_id(f["id"]))

    def test_local_squash_merge_into_base(self):
        fid, fb = self._ready_feature("local")
        with self.store() as s:
            res = feature.merge(s, s.feature_by_id(fid))
            self.assertEqual(res, {"ok": True, "pr": None, "phase": "done"})
            self.assertEqual(s.feature_by_id(fid)["phase"], "done")
        # The feature's file is now on main as one squash commit (main was checked out).
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "feat.txt")))
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_local_merge_refuses_a_dirty_checkout(self):
        """The base is the folder's own checkout: merge refuses when it has uncommitted work and
        never touches it (the person's edits are safe)."""
        fid, fb = self._ready_feature("local")
        with open(os.path.join(self.repo, "README.md"), "a") as fh:
            fh.write("uncommitted edit\n")
        with self.store() as s:
            with self.assertRaises(Fail):
                feature.merge(s, s.feature_by_id(fid))
            self.assertEqual(s.feature_by_id(fid)["phase"], "ready")  # not moved to done
        with open(os.path.join(self.repo, "README.md")) as fh:
            self.assertIn("uncommitted edit", fh.read())  # the person's edit is untouched

    def test_remote_refuses_without_a_pull_request(self):
        """A remote Feature with no pull request must not fall back to a local merge and report done."""
        called = []
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="remote")
            f["phase"] = "ready"  # no pr recorded
            fid = f["id"]
        self._patch(feature, "gh", lambda *a, **k: (called.append(a), (True, "", ""))[1])
        with self.store() as s:
            with self.assertRaises(Fail):
                feature.merge(s, s.feature_by_id(fid))
            self.assertEqual(s.feature_by_id(fid)["phase"], "ready")
        self.assertEqual(called, [])  # never shelled a local merge or a gh call

    def test_remote_merges_the_pull_request(self):
        calls = []
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="remote")
            f["phase"] = "ready"
            f["pr"] = {"number": 7, "url": "https://github.com/acme/shop/pull/7", "repo": "acme/shop"}
            fid = f["id"]
        self._patch(feature, "gh", lambda *a, **k: (calls.append(a), (True, "", ""))[1])
        with self.store() as s:
            res = feature.merge(s, s.feature_by_id(fid))
        self.assertEqual(res["pr"], "https://github.com/acme/shop/pull/7")
        self.assertEqual(res["phase"], "done")
        self.assertEqual(calls[0], ("pr", "merge", "7", "--repo", "acme/shop", "--squash"))


# ---- fix round ------------------------------------------------------------------------------

class FixRound(Base):
    def test_adds_a_round_and_a_verify_task(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            f["phase"] = "verifying"
            f["attention"] = "verify-open"
            fid = f["id"]
            res = feature.fix_round(s, f)
            self.assertEqual(res["fix_rounds"], 1)
            verify = s.task_by_id(s._num_for(res["verify_task"]))
            self.assertEqual(verify["role"], "verify")
            self.assertEqual(verify["feature"], fid)
            self.assertEqual(f["phase"], "verifying")
            self.assertEqual(f["attention"], "")
            self.assertEqual(f["verify_task"], verify["id"])

    def test_counts_up_each_round(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            f["phase"], f["attention"], f["fix_rounds"] = "verifying", "verify-open", 1
            res = feature.fix_round(s, f)
            self.assertEqual(res["fix_rounds"], 2)

    def test_refused_without_verify_open(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            f["phase"], f["attention"] = "building", ""
            with self.assertRaises(Fail):
                feature.fix_round(s, f)


# ---- get ------------------------------------------------------------------------------------

class Get(Base):
    def test_resolves_feature_and_rejects_a_task(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            self.assertEqual(feature.get(s, s.ref(f))["id"], f["id"])
            spec_ref = s.ref(s.task_by_id(f["spec_task"]))
            with self.assertRaises(Fail):
                feature.get(s, spec_ref)  # a Task ref is not a Feature


# ---- the `feature` command --------------------------------------------------------------------

class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Command(Base):
    def _args(self, feature_cmd, **kw):
        kw.setdefault("folder", self.repo)
        kw.setdefault("json", False)
        return Args(feature_cmd=feature_cmd, **kw)

    def test_add_local_prints_the_warning_and_queues_the_spec(self):
        args = self._args("add", title="Login flow", base=None, change=None, mode=None, auto_fanout=True)
        out = io.StringIO()
        with redirect_stdout(out):
            feature.cmd_feature(args)
        self.assertIn("No GitHub remote: local mode, one machine only", out.getvalue())
        with self.store(write=False) as s:
            f = s.data["features"][0]
            self.assertEqual(f["phase"], "planning")
            spec = s.task_by_id(f["spec_task"])
            self.assertEqual(spec["state"], "queued")
            self.assertEqual(spec["queue"]["argv"], ["claude"])

    def test_add_respects_forced_remote_mode(self):
        self._patch(feature, "detect_mode", lambda folder: "local")
        args = self._args("add", title="Login flow", base=None, change=None, mode="remote",
                          auto_fanout=True)
        with redirect_stdout(io.StringIO()):
            feature.cmd_feature(args)
        with self.store(write=False) as s:
            self.assertEqual(s.data["features"][0]["mode"], "remote")

    def test_list_shows_every_feature(self):
        with self.store() as s:
            feature.create(s, "Login flow", mode="local")
            feature.create(s, "Checkout polish", mode="local")
        args = self._args("list", json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            feature.cmd_feature(args)
        import json
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["features"]), 2)
        self.assertTrue(all("ref" in f for f in data["features"]))

    def test_show_json(self):
        with self.store() as s:
            f = feature.create(s, "Login flow", mode="local")
            ref = s.ref(f)
        args = self._args("show", ref=ref, on=None, json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            feature.cmd_feature(args)
        import json
        data = json.loads(out.getvalue())
        self.assertEqual(data["ref"], ref)
        self.assertEqual(data["children"], [])

    def test_unknown_subcommand(self):
        with self.assertRaises(Fail):
            feature.cmd_feature(self._args(None))


if __name__ == "__main__":
    unittest.main()
