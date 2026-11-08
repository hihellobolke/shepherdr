"""Fake `hc` and `gh` for the OpenSpec flow end-to-end tests (task T23h).

The end-to-end tests (T23r remote, T23l local) run real `herdr-task` against a throwaway repo.
Those tests must never reach the person's real herdr-core (`hc`), their real GitHub (`gh`) or their
real home.
This module builds the stand-ins they use.

It writes two executables, a fake `hc` and a fake `gh`, into a scratch `bin/` directory that the
tests put first on `PATH` (and it points `HERDR_TASK_GH` at the fake `gh`, which `base.py` reads).
Both fakes record every call they get and simulate only what the flow uses:

- a child agent that commits to its task branch and runs `herdr-task ready` (the fake `hc run`
  starts it, so a real worker CLI is never launched);
- pull requests with checks, listed and viewed as `gh` prints them;
- a squash merge into a bare remote, the same shape as a GitHub squash merge;
- a second machine, reached through `hc exec --on <machine>`, running its own `herdr-task` on its
  own store (the pattern of `AutomationFromAnotherMachine` in `test_herdr_task.py`).

Each test can script, per child: a forced conflict, a failed child and a risky child.

The harness object (`FlowFakes`) owns a scratch directory and never writes outside it.
Everything the fakes share lives in one JSON state file (`FLOWFAKES_STATE`), guarded by a file
lock, so concurrent background agents stay consistent.
The same module runs as the fake executables (`python3 -I flowfakes.py hc|gh|agent ...`); the
harness API and the fakes therefore read and write the state through the same code.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time


# ---- paths and state --------------------------------------------------------------------------

SELF = os.path.realpath(__file__)

# A git identity carried in the environment, and global/system git config turned off, so the fakes
# commit and merge in a scratch HOME with no git config at all (git checks the committer identity
# before a merge, not only at commit time) and a person's own ~/.gitconfig never leaks in.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Flow Fake", "GIT_AUTHOR_EMAIL": "flow@example.invalid",
    "GIT_COMMITTER_NAME": "Flow Fake", "GIT_COMMITTER_EMAIL": "flow@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

# A child agent's default behaviour when a test does not script one for its name.
DEFAULT_AGENT = {"commit": True, "ready": True, "fail": False, "conflict": False, "risky": False,
                 "owns": None, "message": None, "ref": None}


def _state_dir():
    d = os.environ.get("FLOWFAKES_STATE")
    if not d:
        raise RuntimeError("FLOWFAKES_STATE is not set: run through FlowFakes.env()")
    return d


@contextlib.contextmanager
def _locked(state_dir):
    """The state file, open for read-modify-write under an exclusive lock. Yields the loaded dict;
    whatever it holds on exit is written back with an atomic replace, so a concurrent lock-free
    reader (a background agent, a pull-request query) never sees a half-written file."""
    lock = os.path.join(state_dir, "lock")
    path = os.path.join(state_dir, "state.json")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with open(path) as f:
            state = json.load(f)
        box = {"state": state}
        yield box
        _write_state(state_dir, box["state"])


def _write_state(state_dir, state):
    """Replace the state file atomically (write a sibling temp, then `os.replace`)."""
    path = os.path.join(state_dir, "state.json")
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_state(state_dir):
    # No lock: writers replace the file atomically, so a read sees either the whole old or the whole
    # new state, never a torn one.
    with open(os.path.join(state_dir, "state.json")) as f:
        return json.load(f)


def _record(state_dir, prog, argv, cwd):
    """Append one call to the call log (its own file, so it never contends with the state lock)."""
    line = json.dumps({"prog": prog, "argv": list(argv), "cwd": cwd, "time": time.time()})
    path = os.path.join(state_dir, "calls.jsonl")
    with open(os.path.join(state_dir, "calls.lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with open(path, "a") as f:
            f.write(line + "\n")


# ---- git helpers (used by the fakes and the harness) ------------------------------------------

def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *args], env={**os.environ, **_GIT_ENV},
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _sha(origin, branch):
    """The tip of `branch` in the bare origin, or None when it has no such branch yet."""
    r = _git(origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return r.stdout.strip() or None


# ================================================================================================
# The harness
# ================================================================================================

class FlowFakes:
    """Builds the fakes in a scratch directory and drives them from the test.

    Typical use:

        with tempfile.TemporaryDirectory() as root:
            fakes = FlowFakes(root).start()
            env = fakes.env()            # PATH, HERDR_TASK_GH, FLOWFAKES_STATE, a scratch HOME
            ...                          # run herdr-task / git with env
            assert fakes.calls("gh")     # what gh was asked
    """

    def __init__(self, root, *, repo="octo/demo", default_branch="main", prefix="HAT"):
        self.root = os.path.realpath(root)
        self.repo = repo
        self.default_branch = default_branch
        self.prefix = prefix
        self.bin_dir = os.path.join(self.root, "bin")
        self.state_dir = os.path.join(self.root, "state")
        self.origin = os.path.join(self.root, "origin.git")
        self.home_repo = os.path.join(self.root, "repo")
        self.home = os.path.join(self.root, "home")
        self._gh_work = os.path.join(self.state_dir, "gh-work")
        self._started = False

    # -- lifecycle --

    def start(self):
        """Create the bin directory, the fakes, the state file, the bare remote and the home repo."""
        for d in (self.bin_dir, self.state_dir, self.home):
            os.makedirs(d, exist_ok=True)
        self._write_fakes()
        self._init_repo()
        state = {
            "repo": self.repo,
            "default_branch": self.default_branch,
            "origin": self.origin,
            "gh_work": self._gh_work,
            "pr_counter": 0,
            "prs": [],
            "default_check": "SUCCESS",
            "machines": {},
            "agents": {},
            "ran": {},
        }
        with open(os.path.join(self.state_dir, "state.json"), "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        open(os.path.join(self.state_dir, "calls.jsonl"), "w").close()
        self._started = True
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        return False

    def _write_fakes(self):
        py = sys.executable
        for name in ("hc", "gh"):
            path = os.path.join(self.bin_dir, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexec "{py}" -I "{SELF}" {name} "$@"\n')
            os.chmod(path, 0o755)

    def _init_repo(self):
        _git(self.root, "init", "-q", "--bare", "-b", self.default_branch, self.origin)
        _git(self.root, "clone", "-q", self.origin, self.home_repo)
        # A first commit so the base branch exists on the remote.
        readme = os.path.join(self.home_repo, "README.md")
        with open(readme, "w") as f:
            f.write("# demo\n")
        _git(self.home_repo, "add", "README.md")
        _git(self.home_repo, "commit", "-q", "-m", "first")
        _git(self.home_repo, "push", "-q", "origin", self.default_branch)
        # The merge work-clone (self._gh_work) is created on the first squash merge, so a test that
        # never merges pays nothing for it.

    # -- environment --

    def env(self, extra=None, *, home=None, store=None, settings=None):
        """The environment a test runs `herdr-task` or `git` under: the fakes first on `PATH`,
        `HERDR_TASK_GH` at the fake `gh`, `FLOWFAKES_STATE` at the state directory, and the store and
        settings redirected into the scratch root so nothing touches the real ones.

        `HOME` is left as the real one by default, on purpose: `base.guard` refuses every `hc` call
        when `HOME` is not the account's own (it checks the environment before it even resolves the
        fake on `PATH`), so a changed `HOME` would stop the real `herdr-task` from running `hc` at
        all. Isolation comes from the redirected store, settings and `HERDR_TASK_GH` and from the
        fakes being `hc` and `gh`, not from a changed `HOME`. Pass `home=` only to test the fakes
        themselves in isolation (they read `FLOWFAKES_STATE`, never `HOME`).

        This redirects every writable path the real `herdr-task` derives from `HOME` that has an
        environment override (`HERDR_TASK_STORE`, `HERDR_TASK_WORKTREES`, `HERDR_TASK_SETTINGS`,
        `HERDR_TASK_TEMPLATES`), so a normal Task start, which can create or upgrade worktrees and
        role templates, writes under the scratch root and never the real `~/.herdr` or
        `~/.config/herdr-core`, even though `HOME` stays real for `base.guard`."""
        env = dict(os.environ)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["HERDR_TASK_GH"] = os.path.join(self.bin_dir, "gh")
        env["FLOWFAKES_STATE"] = self.state_dir
        if home is not None:
            env["HOME"] = home
        env["HERDR_TASK_STORE"] = store or os.path.join(self.root, "store")
        env["HERDR_TASK_SETTINGS"] = settings or os.path.join(self.root, "shepherdr.json")
        env["HERDR_TASK_WORKTREES"] = os.path.join(self.root, "worktrees")  # base.WORKTREES reads this
        env["HERDR_TASK_TEMPLATES"] = os.path.join(self.root, "templates")  # settings.TEMPLATES reads this
        env.update(_GIT_ENV)  # a git identity and no global/system config, so git stays isolated too
        if extra:
            env.update(extra)
        return env

    # -- call log --

    def calls(self, prog=None):
        """Every recorded call, oldest first, each `{prog, argv, cwd, time}`; `prog` filters."""
        path = os.path.join(self.state_dir, "calls.jsonl")
        out = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return [c for c in out if prog is None or c["prog"] == prog]

    def last_call(self, prog=None):
        calls = self.calls(prog)
        return calls[-1] if calls else None

    def reset_calls(self):
        open(os.path.join(self.state_dir, "calls.jsonl"), "w").close()

    # -- GitHub control and inspection --

    def prs(self):
        return _read_state(self.state_dir)["prs"]

    def find_pr(self, *, number=None, head=None, state=None):
        for pr in self.prs():
            if number is not None and pr["number"] != number:
                continue
            if head is not None and pr["head"] != head:
                continue
            if state is not None and pr["state"] != state:
                continue
            return pr
        return None

    def open_pr(self, head, *, base=None, title="", body=""):
        """Open a pull request for a branch already pushed to the remote, as `gh pr create` would.
        Returns the record."""
        base = base or self.default_branch
        with _locked(self.state_dir) as box:
            pr = _new_pr(box["state"], head, base, title, body)
        return pr

    def set_checks(self, number, conclusion):
        """Set a pull request's checks to `SUCCESS`, `FAILURE` or `PENDING` (the statusCheckRollup
        `gh pr view` prints)."""
        with _locked(self.state_dir) as box:
            _pr_by_number(box["state"], number)["checks"] = conclusion

    def set_mergeable(self, number, value):
        """Force a pull request's mergeable flag (`MERGEABLE`, `CONFLICTING`, `UNKNOWN`). A scripted
        `CONFLICTING` is sticky: it refuses every merge until it is cleared, the forced-conflict
        scenario for the closing step. (A conflict that the git merge itself hits is not sticky: a
        later merge of a resolved head still goes through.)"""
        with _locked(self.state_dir) as box:
            pr = _pr_by_number(box["state"], number)
            pr["mergeable"] = value
            pr["force_conflict"] = value == "CONFLICTING"

    def base_sha(self, branch=None):
        return _sha(self.origin, branch or self.default_branch)

    # -- machines --

    def add_machine(self, name, *, store=None, home=None):
        """Register a second machine reached through `hc exec --on <name>`. It runs its own
        `herdr-task` on its own store and home, all inside the scratch directory."""
        mhome = home or os.path.join(self.root, "machines", name)
        os.makedirs(mhome, exist_ok=True)
        mstore = store or os.path.join(mhome, ".herdr", "tasks")
        os.makedirs(mstore, exist_ok=True)
        rec = {"name": name, "store": mstore, "home": mhome, "down": False}
        with _locked(self.state_dir) as box:
            box["state"]["machines"][name] = rec
        return rec

    def set_down(self, name, down=True):
        """A machine the home machine cannot reach: its `hc exec` answers connection refused."""
        with _locked(self.state_dir) as box:
            box["state"]["machines"][name]["down"] = down

    def machine_store(self, name):
        return _read_state(self.state_dir)["machines"][name]["store"]

    # -- fake child agents --

    def script_agent(self, key, *, commit=True, ready=True, fail=False, conflict=False,
                     risky=False, owns=None, message=None, ref=None):
        """Script what the child agent named `key` (the `hc run --name`) does when it is started.

        - `commit`: make a commit on the task branch touching an owned path (default on).
        - `ready`: run `herdr-task ready <ref>` after committing (default on).
        - `fail`: the child fails (no ready; the run exits non-zero): the failed-child scenario.
        - `conflict`: write content that conflicts with the base branch: the forced-conflict
          scenario at merge time.
        - `risky`: the child is a Risky Task. The real flow gives a high-risk Task `merge=ask`, so
          it must not merge on its own: the agent does its work and runs `ready`, but it does not ask
          for a merge and records that it is awaiting the person's Accept (`ran()` reads it). The E2E
          sets `--risk high` at fan-out and then simulates the Accept with an explicit merge.
        - `owns`: the path(s) the commit touches (default `<key>.txt`).
        - `message`: the commit message (default `"<key> work"`).
        - `ref`: the ref the agent uses for `ready`, when the `hc run` branch does not carry it.
        """
        rec = {"commit": commit, "ready": ready, "fail": fail, "conflict": conflict, "risky": risky,
               "owns": [owns] if isinstance(owns, str) else owns, "message": message, "ref": ref}
        with _locked(self.state_dir) as box:
            box["state"]["agents"][key] = rec
        return rec

    def ran(self, key):
        """What the agent named `key` did when it last ran: `{ref, risky, awaiting_accept, ready}`,
        or `None` if it has not run. A risky child records `awaiting_accept` true."""
        return _read_state(self.state_dir).get("ran", {}).get(key)

    def run_agent(self, key, *, worktree, branch, ref, base=None, folder=None, wait=True, env=None):
        """Start the child agent `key` directly (what the fake `hc run` does for it), for a test
        that drives one agent without the whole `hc run` argv. Returns the finished process when
        `wait`, else the `Popen`."""
        base = base or self.default_branch
        args = [sys.executable, "-I", SELF, "agent", key, "--worktree", worktree, "--branch", branch,
                "--ref", ref, "--base", base]
        if folder:
            args += ["--folder", folder]
        runenv = env or self.env()
        if wait:
            return subprocess.run(args, env=runenv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True)
        return subprocess.Popen(args, env=runenv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)


# ---- pull request records (shared by the harness and the fake gh) -----------------------------

def _pr_url(repo, number):
    return f"https://github.com/{repo}/pull/{number}"


def _new_pr(state, head, base, title, body):
    state["pr_counter"] += 1
    n = state["pr_counter"]
    pr = {
        "number": n,
        "url": _pr_url(state["repo"], n),
        "repo": state["repo"],
        "head": head,
        "base": base,
        "title": title,
        "body": body,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "force_conflict": False,
        "headRefOid": _sha(state["origin"], head) or "",
        "mergeCommit": None,
        "checks": state.get("default_check", "SUCCESS"),
        "comments": [],
    }
    state["prs"].append(pr)
    return pr


def _pr_by_number(state, number):
    for pr in state["prs"]:
        if pr["number"] == int(number):
            return pr
    raise RuntimeError(f"no pull request #{number}")


def _rollup(pr):
    """The statusCheckRollup list `gh pr view --json statusCheckRollup` returns, from the pull
    request's single check state."""
    c = pr.get("checks") or "SUCCESS"
    if c == "PENDING":
        return [{"__typename": "CheckRun", "name": "check", "status": "IN_PROGRESS", "conclusion": ""}]
    return [{"__typename": "CheckRun", "name": "check", "status": "COMPLETED", "conclusion": c}]


def _live_head(state, pr):
    """A pull request with its head sha and mergeable flag refreshed from the remote while it is open,
    so a child that rebased or pushed corrections after the pull request opened is reflected (what
    `gh pr view` returns live): once a conflicting head is resolved and pushed, `mergeable` goes back
    to `MERGEABLE`. A scripted conflict (`force_conflict`) stays `CONFLICTING`. A merged pull request
    keeps its head sha and flag frozen at the moment it merged."""
    if pr["state"] != "OPEN":
        return pr
    live = {**pr, "headRefOid": _sha(state["origin"], pr["head"]) or pr["headRefOid"]}
    if not pr.get("force_conflict"):
        live["mergeable"] = _mergeable_now(state, pr)
    return live


def _mergeable_now(state, pr):
    """Whether the head still merges cleanly into the base, by a dry `git merge-tree` on the bare
    origin (0 clean, non-zero on conflict)."""
    r = _git(state["origin"], "merge-tree", "--write-tree", pr["base"], pr["head"], check=False)
    return "MERGEABLE" if r.returncode == 0 else "CONFLICTING"


def _pr_json(pr, fields):
    """One pull request as the JSON `gh` prints for the asked `fields`."""
    out = {}
    for fld in fields:
        if fld == "statusCheckRollup":
            out[fld] = _rollup(pr)
        elif fld == "isCrossRepository":
            out[fld] = False
        elif fld == "mergeCommit":
            out[fld] = {"oid": pr["mergeCommit"]} if pr["mergeCommit"] else None
        elif fld == "baseRefName":
            out[fld] = pr["base"]
        elif fld == "headRefName":
            out[fld] = pr["head"]
        elif fld in pr:
            out[fld] = pr[fld]
    return out


# ================================================================================================
# The fakes (run as `python3 -I flowfakes.py hc|gh|agent ...`)
# ================================================================================================

def _parse_flags(argv, with_values, bare=()):
    """A tiny argv parser: `with_values` flags take the next token, `bare` flags are booleans.
    Returns `(flags, positionals, after_dashdash)`."""
    flags, pos, rest = {}, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest = argv[i + 1:]
            break
        if a in with_values:
            flags[a] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a in bare:
            flags[a] = True
            i += 1
            continue
        pos.append(a)
        i += 1
    return flags, pos, rest


# ---- fake gh ----------------------------------------------------------------------------------

def _fake_gh(argv):
    state_dir = _state_dir()
    _record(state_dir, "gh", argv, os.getcwd())
    if not argv:
        return 0
    if argv[0] == "repo" and len(argv) > 1 and argv[1] == "view":
        return _gh_repo_view(state_dir, argv[2:])
    if argv[0] == "pr":
        return _gh_pr(state_dir, argv[1:])
    if argv[0] == "issue":
        return _gh_issue(state_dir, argv[1:])
    if argv[0] == "api":
        print("{}")
        return 0
    return 0


def _jq_field(obj, q):
    """Enough of `-q` for the flow: a single `.field` path on a dict."""
    q = q.strip()
    if q.startswith("."):
        q = q[1:]
    if q == "":
        return obj
    cur = obj
    for part in q.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _gh_repo_view(state_dir, argv):
    state = _read_state(state_dir)
    flags, _, _ = _parse_flags(argv, {"--json", "-q", "--jq"})
    data = {"nameWithOwner": state["repo"]}
    q = flags.get("-q") or flags.get("--jq")
    if q:
        val = _jq_field(data, q)
        print("" if val is None else val)
    else:
        print(json.dumps(data))
    return 0


def _gh_pr(state_dir, argv):
    sub = argv[0] if argv else ""
    if sub == "create":
        return _gh_pr_create(state_dir, argv[1:])
    if sub == "list":
        return _gh_pr_list(state_dir, argv[1:])
    if sub == "view":
        return _gh_pr_view(state_dir, argv[1:])
    if sub == "comment":
        return _gh_pr_comment(state_dir, argv[1:])
    if sub == "merge":
        return _gh_pr_merge(state_dir, argv[1:])
    return 0


def _gh_pr_create(state_dir, argv):
    flags, _, _ = _parse_flags(argv, {"--repo", "--head", "--base", "--title", "--body"})
    with _locked(state_dir) as box:
        state = box["state"]
        head = flags.get("--head")
        existing = next((p for p in state["prs"] if p["head"] == head and p["state"] == "OPEN"), None)
        pr = existing or _new_pr(state, head, flags.get("--base") or state["default_branch"],
                                 flags.get("--title") or "", flags.get("--body") or "")
    # gh prints the URL of the created pull request; github.py reads out.split()[-1].
    print(pr["url"])
    return 0


def _gh_pr_list(state_dir, argv):
    flags, _, _ = _parse_flags(argv, {"--repo", "--head", "--state", "--json", "--limit"})
    state = _read_state(state_dir)
    want = (flags.get("--state") or "open").upper()
    head = flags.get("--head")
    fields = (flags.get("--json") or "").split(",") if flags.get("--json") else []
    rows = []
    for pr in state["prs"]:
        if head is not None and pr["head"] != head:
            continue
        if want != "ALL" and pr["state"] != want:
            continue
        pr = _live_head(state, pr)
        rows.append(_pr_json(pr, fields) if fields else {"number": pr["number"]})
    print(json.dumps(rows))
    return 0


def _gh_pr_view(state_dir, argv):
    flags, pos, _ = _parse_flags(argv, {"--repo", "--json", "-q", "--jq"})
    number = next((p for p in pos if p.isdigit()), None)
    state = _read_state(state_dir)
    try:
        pr = _pr_by_number(state, number)
    except RuntimeError:
        sys.stderr.write(f"no pull request found for {number}\n")
        return 1
    pr = _live_head(state, pr)
    fields = (flags.get("--json") or "").split(",") if flags.get("--json") else []
    data = _pr_json(pr, fields) if fields else pr
    q = flags.get("-q") or flags.get("--jq")
    if q:
        val = _jq_field(data, q)
        print("" if val is None else val)
    else:
        print(json.dumps(data))
    return 0


def _gh_pr_comment(state_dir, argv):
    flags, pos, _ = _parse_flags(argv, {"--repo", "--body", "--body-file"})
    number = next((p for p in pos if p.isdigit()), None)
    body = flags.get("--body")
    if body is None and flags.get("--body-file"):
        with open(flags["--body-file"]) as f:
            body = f.read()
    with _locked(state_dir) as box:
        pr = _pr_by_number(box["state"], number)
        pr["comments"].append(body or "")
        url = pr["url"]
    print(url + "#comment")
    return 0


def _gh_pr_merge(state_dir, argv):
    flags, pos, _ = _parse_flags(
        argv, {"--repo", "--match-head-commit"},
        bare=("--squash", "--merge", "--rebase", "--delete-branch", "--auto"))
    number = next((p for p in pos if p.isdigit()), None)
    with _locked(state_dir) as box:
        state = box["state"]
        pr = _pr_by_number(state, number)
        if pr["state"] == "MERGED":
            print(f"Pull request #{number} already merged")
            return 0
        if pr.get("force_conflict"):
            sys.stderr.write(f"Pull request #{number} is not mergeable: the merge commit cannot be cleanly created\n")
            return 1
        current_head = _sha(state["origin"], pr["head"]) or pr["headRefOid"]
        want = flags.get("--match-head-commit")
        # GitHub refuses the merge when the branch moved since the caller last saw it (the sha it
        # passes no longer matches the tip); the caller re-reads and retries.
        if want and want != current_head:
            sys.stderr.write(f"Pull request #{number} head has changed: expected {want}, found {current_head}\n")
            return 1
        ok, merge_commit, conflicted = _squash_merge(state, pr)
        if not ok:
            # A conflict the git merge itself hits, not scripted: recorded for display, but not sticky,
            # so a later merge of a resolved head still succeeds.
            pr["mergeable"] = "CONFLICTING"
            sys.stderr.write("failed to merge: conflicts in " + ", ".join(conflicted) + "\n")
            return 1
        pr["state"] = "MERGED"
        pr["mergeable"] = "MERGEABLE"
        pr["mergeCommit"] = merge_commit
        pr["headRefOid"] = current_head  # freeze the head that was merged, so later queries are stable
    print(f"Merged pull request #{number} ({merge_commit[:7]})")
    return 0


def _squash_merge(state, pr):
    """Squash-merge the pull request's head into its base in the bare origin, the shape of a GitHub
    squash merge: one commit on the base, the head branch left as it was. Returns
    `(ok, merge_commit, conflicted_files)`."""
    work, origin = state["gh_work"], state["origin"]
    base, head = pr["base"], pr["head"]
    if not os.path.isdir(work):
        _git(os.path.dirname(work), "clone", "-q", origin, work)
    _git(work, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _git(work, "checkout", "-q", "-B", base, f"refs/remotes/origin/{base}")
    r = _git(work, "merge", "--squash", "--no-commit", f"refs/remotes/origin/{head}", check=False)
    if r.returncode != 0:
        conflicted = [ln[3:] for ln in _git(work, "status", "--porcelain").stdout.splitlines()
                      if ln[:2] in ("UU", "AA", "DD", "DU", "UD", "AU", "UA")]
        _git(work, "merge", "--abort", check=False)
        _git(work, "reset", "-q", "--hard", f"refs/remotes/origin/{base}", check=False)
        return False, None, conflicted
    title = pr.get("title") or f"Merge #{pr['number']}"
    _git(work, "commit", "-q", "--allow-empty", "-m", f"{title} (#{pr['number']})")
    _git(work, "push", "-q", "origin", f"{base}:{base}")
    return True, _git(work, "rev-parse", "HEAD").stdout.strip(), []


def _gh_issue(state_dir, argv):
    """Minimal issue support (herdr-task feedback uses it): record and print a plausible url."""
    sub = argv[0] if argv else ""
    state = _read_state(state_dir)
    if sub == "list":
        print("[]")
        return 0
    if sub in ("create", "comment"):
        print(f"https://github.com/{state['repo']}/issues/1")
        return 0
    return 0


# ---- fake hc ----------------------------------------------------------------------------------

def _fake_hc(argv):
    state_dir = _state_dir()
    _record(state_dir, "hc", argv, os.getcwd())
    if not argv:
        return 0
    cmd = argv[0]
    if cmd == "run":
        return _hc_run(state_dir, argv[1:])
    if cmd == "exec":
        return _hc_exec(state_dir, argv[1:])
    if cmd == "file":
        return _hc_file(state_dir, argv[1:])
    if cmd == "machine":
        return _hc_machine(state_dir, argv[1:])
    if cmd in ("wait", "kill", "label", "send", "keys"):
        return 0
    if cmd == "read":
        return 0
    if cmd == "get":
        if "--json" in argv:
            print("null")
        return 0
    if cmd in ("server", "status"):
        if "--json" in argv:
            print(json.dumps({"name": "home"}))
        return 0
    return 0


def _hc_run(state_dir, argv):
    """Start a fake child agent in the background instead of a real worker CLI. Prints the address
    JSON that `launch.py` reads, then detaches the agent."""
    flags, _, after = _parse_flags(
        argv, {"--name", "--cwd", "--label", "--parent"},
        bare=("--bg", "--json", "--keep", "--no-parent", "--outlive-parent"))
    name = flags.get("--name") or "agent"
    cwd = flags.get("--cwd") or os.getcwd()
    ref, branch, base, folder = _agent_target(cwd, state_dir, name)
    args = [sys.executable, "-I", SELF, "agent", name, "--worktree", cwd, "--branch", branch,
            "--ref", ref, "--base", base, "--folder", folder]
    subprocess.Popen(args, env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    if flags.get("--json"):
        print(json.dumps({"address": name, "name": name}))
    return 0


# A ref is an upper-case prefix and a number (HAT-14, SHMD-2). The branch's title slug is lower-cased
# (feature.slug_title), so matching upper-case only never mistakes a title word like "http-500" for a
# ref.
REF_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]*-\d+(?![A-Za-z0-9])")


def ref_from_branch(branch):
    """The child ref encoded in a branch name. A child branch carries the feature ref and then the
    child ref (`task/HAT-12-HAT-14-parser`, C5), so the child is the last ref token, not the first; a
    classic branch (`task/HAT-14-title`) and a feature branch (`feature/HAT-12-login-flow`) have one,
    so the last is also the right one. Only upper-case ref tokens count, so a lower-cased title slug
    with ref-like text (`task/HAT-12-HAT-14-handle-http-500`) still resolves to the child ref
    (`HAT-14`). Returns the branch unchanged when it holds no ref token."""
    tokens = REF_TOKEN.findall(branch)
    return tokens[-1] if tokens else branch


def _agent_target(cwd, state_dir, name):
    """Work out the ref, branch, base and folder for the agent the run starts. A test that scripts
    the agent by `name` with an explicit `ref` gets exactly that; otherwise the ref is read from the
    worktree's branch (the child ref, C5), which is what `herdr-task ready` needs."""
    branch = "HEAD"
    r = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "HEAD":
        branch = r.stdout.strip()
    state = _read_state(state_dir)
    base = state["default_branch"]
    scripted = (state["agents"].get(name) or {}).get("ref")
    ref = scripted or ref_from_branch(branch)
    return ref, branch, base, cwd


def _hc_exec(state_dir, argv):
    """`hc exec --on <machine> [--timeout T] -- <cmd...>`: run the command as if on that machine, or
    answer connection refused when the machine is down.

    The command runs with the machine's own `HOME` (its scratch directory), working directory, store
    and settings, all under the scratch root, so everything a remote command reaches stays on that
    simulated machine and never on the real one: `cli.remote_run` launches the remote `herdr-task`
    through `"$HOME/.local/bin/herdr-task"` and does `test -d .config/...` and `chmod`, which must
    resolve to the copy and paths on that machine. The flow's remote command, `feature-status`, reads
    the machine's store and does not call `hc`, so `base.guard` (which refuses `hc` under a HOME that
    is not the account's own) is not reached; a remote command that did call `hc` could not be
    simulated on one box and is out of scope."""
    flags, _, after = _parse_flags(argv, {"--on", "--timeout"}, bare=("--stdin", "--observe"))
    machine = flags.get("--on")
    state = _read_state(state_dir)
    if machine:
        rec = state["machines"].get(machine)
        if rec is None:
            # An unregistered machine is never run here (that would use the real HOME and could launch
            # the real installed herdr-task): it is refused, as hc would for a machine it cannot reach.
            sys.stderr.write(f"hc: {machine}: no such machine\n")
            return 1
        if rec.get("down"):
            sys.stderr.write(f"hc: {machine}: connection refused\n")
            return 1
        home = rec["home"]
        env = dict(os.environ)
        env["HOME"] = home
        env["HERDR_TASK_STORE"] = rec["store"]
        env["HERDR_TASK_SETTINGS"] = os.path.join(home, ".herdr", "shepherdr.json")
        env["HERDR_TASK_WORKTREES"] = os.path.join(home, ".herdr", "worktrees")
        env["HERDR_TASK_TEMPLATES"] = os.path.join(home, ".config", "herdr-core", "templates")
        stdin = sys.stdin.read() if flags.get("--stdin") else None
        r = subprocess.run(after, env=env, cwd=home, input=stdin, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    # No `--on`: a local exec on the home machine (the caller's own controlled environment).
    if after:
        r = subprocess.run(after, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    return 0


def _hc_file(state_dir, argv):
    """`hc file write --on <machine> <target>`: write stdin to the target under the machine's home.

    The target is always resolved under that machine's scratch home, and a target that would escape
    it (an absolute path, a `..` climb, a symlink out) is refused: a fake must never write outside
    the scratch directory. The body is copied as bytes, so the real binary `herdr-task` zipapp
    (`base.bundle`, sent by `cli.remote_run`) transfers intact."""
    if not argv or argv[0] != "write":
        return 0
    flags, pos, _ = _parse_flags(argv[1:], {"--on"})
    machine = flags.get("--on")
    target = pos[0] if pos else None
    body = sys.stdin.buffer.read()
    state = _read_state(state_dir)
    if machine and machine in state["machines"] and target:
        home = os.path.realpath(state["machines"][machine]["home"])
        dest = os.path.realpath(os.path.join(home, target))
        # A target is a relative path under the machine home (cli.remote_run writes
        # `.local/bin/herdr-task`). Refuse an absolute path or one that climbs or links out.
        if os.path.isabs(target) or (dest != home and not dest.startswith(home + os.sep)):
            sys.stderr.write(f"hc file write: refusing a path outside {machine}: {target}\n")
            return 1
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(body)
    return 0


def _hc_machine(state_dir, argv):
    """`hc machine ls --json`: the registered machines, the shape `connected_machines` reads."""
    state = _read_state(state_dir)
    if argv and argv[0] == "ls":
        if "--json" in argv:
            machines = [{"name": n, "state": "connected"} for n in sorted(state["machines"])]
            print(json.dumps({"machines": machines}))
        return 0
    return 0


# ---- fake child agent -------------------------------------------------------------------------

def _fake_agent(argv):
    """A child worker stand-in: it commits to its task branch, pushes it and runs `herdr-task
    ready`, as the flow's child agents do. What it does is scripted per name in the state."""
    flags, pos, _ = _parse_flags(argv, {"--worktree", "--branch", "--ref", "--base", "--folder"})
    key = pos[0] if pos else "agent"
    state_dir = _state_dir()
    _record(state_dir, "agent", argv, flags.get("--worktree") or os.getcwd())
    state = _read_state(state_dir)
    script = {**DEFAULT_AGENT, **(state["agents"].get(key) or {})}
    wt = flags.get("--worktree")
    branch = flags.get("--branch")
    ref = flags.get("--ref") or key
    base = flags.get("--base") or state["default_branch"]
    folder = flags.get("--folder")

    risky = bool(script.get("risky"))
    if script.get("commit") and wt:
        _agent_commit(wt, branch, base, ref, key, script)
    if script.get("fail"):
        _mark_ran(state_dir, key, ref, risky=risky, ready=False, awaiting_accept=False, failed=True)
        sys.stderr.write(f"{ref}: child failed\n")
        return 1
    rc = 0
    ready_ran = bool(script.get("ready") and folder)
    if ready_ran:
        # A child reports done the same way whether or not it is risky. A Risky Task's merge waits
        # for the person because its Task carries merge=ask (set at fan-out with --risk high, in the
        # flow's own code); the agent does not gate the merge itself. The harness records that a risky
        # child is awaiting the person's Accept (observable through `ran`).
        r = subprocess.run(["herdr-task", "ready", ref, "--folder", folder, "--json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        rc = r.returncode
    # ran() records the real outcome: `ready` true only when it was actually run and succeeded,
    # `failed` true when it ran and returned non-zero.
    _mark_ran(state_dir, key, ref, risky=risky, ready=(ready_ran and rc == 0),
              awaiting_accept=risky, failed=(ready_ran and rc != 0))
    return rc


def _mark_ran(state_dir, key, ref, *, risky, ready, awaiting_accept, failed):
    """Record what the agent did, so a test can observe the outcome (the risky child awaiting an
    Accept, the failed child, the normal child)."""
    with _locked(state_dir) as box:
        box["state"].setdefault("ran", {})[key] = {
            "ref": ref, "risky": risky, "ready": ready,
            "awaiting_accept": awaiting_accept, "failed": failed,
        }


def _agent_commit(wt, branch, base, ref, key, script):
    """Make the child's commit on its task branch and push it to the origin. A `conflict` child
    writes to a shared path with content that will clash with the base branch at merge time."""
    if branch and branch != "HEAD":
        cur = _git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        if cur != branch:
            _git(wt, "checkout", "-q", "-B", branch, check=False)
    owns = script.get("owns") or ([f"{key}.txt"] if not script.get("conflict") else ["shared.txt"])
    for rel in owns:
        path = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(path) or wt, exist_ok=True)
        with open(path, "w") as f:
            f.write((script.get("message") or f"{key} work") + "\n" +
                    ("conflict from " + ref + "\n" if script.get("conflict") else ""))
        _git(wt, "add", rel)
    _git(wt, "commit", "-q", "-m", script.get("message") or f"{ref} {key} work", check=False)
    # Push the branch to the origin so a pull request and a squash merge can see it.
    r = _git(wt, "remote", check=False)
    if "origin" in r.stdout.split():
        _git(wt, "push", "-q", "--force-with-lease", "-u", "origin", branch, check=False)


# ---- entry point ------------------------------------------------------------------------------

def _main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: flowfakes.py hc|gh|agent ...\n")
        return 2
    which, rest = argv[1], argv[2:]
    if which == "hc":
        return _fake_hc(rest)
    if which == "gh":
        return _fake_gh(rest)
    if which == "agent":
        return _fake_agent(rest)
    sys.stderr.write(f"flowfakes.py: unknown fake {which}\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
