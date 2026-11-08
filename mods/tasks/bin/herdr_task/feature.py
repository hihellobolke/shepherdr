"""The Feature lifecycle and the `feature` command (proposal T11, contract C7).

A Feature is a record of its own in the store (ADR-0001). It groups many Tasks under one feature
branch and has its own phase and two human actions: Merge feature and Add a fix round. This module
creates a Feature (its record, its feature branch and the first spec Task), reports its full state,
merges the base branch in early, cancels it, merges it and adds a fix round. The phase values are
the seven of `phases.py`, so every phase write goes through it (`_set_phase`).

A classic Task and a classic space never reach this module; the `feature` command is the only entry.
`feature.on_spec_accepted` is the Plan approval hook `commands.cmd_accept` calls when a spec Task is
accepted; it moves the Feature to `building`. It does not fan out: the fan-out trigger lives in
`tick.py` and waits for the spec merge to land on the feature branch (`spec_merged`), because Accept
can return while the spec Task's merge is still pending or only requested.

`phases.py` is built by a parallel task (T11a). It is imported lazily (`_phases`), so this module
loads and its non-phase commands work even before `phases.py` lands; the tests mock it.
"""

import os
import re
import shutil
import sys
import tempfile

from .base import Fail, git, now, run
from .store import open_store
from .output import say
from .github import gh, github, remote_of
from .settings import machine_name, worker_argv
from .launch import base_ref, caller_space, current_base, git_quick
from .agents import live_agents
from .scheduler import ensure_scheduler
from . import digest


# A change name that already begins with one of these verbs is kept; otherwise `add-` is prefixed,
# so "Login flow" becomes "add-login-flow" (the OpenSpec convention).
CHANGE_VERBS = ("add", "update", "remove", "delete", "fix", "refactor", "change",
                "improve", "polish", "rename", "move", "drop", "split")

# The Needs-you reason code (C2) that offers the person the Add a fix round action.
VERIFY_OPEN = "verify-open"

# A git remote whose host is github.com (https, ssh or scp-like), not just any URL that contains the
# word "github" (so a GitLab repo named github-mirror is not mistaken for GitHub).
_GITHUB_HOST = re.compile(r"(?:^|@|//)github\.com(?:[:/]|$)", re.I)


# ---- phases (built in parallel by T11a; imported lazily) -------------------------------------

def _phases():
    from . import phases
    return phases


def _set_phase(feature, phase):
    """Move a Feature to a phase, through `phases.py` (its `PHASES` are the one list of phase
    names). An unknown phase is a programming error and is refused. When `phases.py` has not landed
    yet the write still happens, so this module never blocks on a parallel task."""
    try:
        valid = _phases().PHASES
    except Exception:
        valid = None
    if valid is not None and phase not in valid:
        raise Fail(f"Unknown phase {phase!r}.")
    feature["phase"] = phase


# ---- names ----------------------------------------------------------------------------------

def slug_title(title):
    """The short-title slug used in a branch name: the title in lowercase, non-word runs as one
    dash, at most 40 characters."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:40].strip("-") or "feature"


def change_name(title):
    """The OpenSpec change name generated from a title: its slug, `add-` prefixed unless it already
    starts with a change verb. `feature add --change` overrides it."""
    s = slug_title(title)
    first = s.split("-", 1)[0]
    return s if first in CHANGE_VERBS else f"add-{s}"


# ---- mode (remote or local) -----------------------------------------------------------------

def _has_github_remote(folder):
    """True when any of the folder's remotes points at github.com."""
    for name in git(folder, "remote")[1].split():
        if _GITHUB_HOST.search(git(folder, "remote", "get-url", name)[1]):
            return True
    return False


def detect_mode(folder):
    """`"remote"` when the folder is a git repo with a GitHub remote and `gh` access, else
    `"local"` (C7). Local mode works on one machine only (proposal 6.10). Every remote is checked
    for a github.com host (not a substring), so a repo with a GitHub remote beside another is still
    remote, and a non-GitHub URL that merely contains "github" is not."""
    if not os.path.isdir(folder) or not git(folder, "rev-parse", "--git-dir")[0]:
        return "local"
    if not _has_github_remote(folder):
        return "local"
    return "remote" if gh("auth", "status")[0] else "local"


# ---- create ---------------------------------------------------------------------------------

def _spec_prompt(title, change):
    return (f"Write the OpenSpec change `{change}` for the Feature \"{title}\" with the person, and "
            f"add its work table to openspec/changes/{change}/tasks.md. Your brief's OpenSpec flow "
            f"section has the path to pick and the work-table format.")


def create(s, title, base=None, change=None, mode=None, auto_fanout=True):
    """Create a Feature: its record, its feature branch and its first spec Task; return the record
    (C7). The feature branch is `feature/<PREFIX>-<NUM>-<slug>`, branched from `base` (the branch
    checked out in the folder when `base` is not given). The spec Task is a worktree Task whose base
    is the feature branch, so its children branch from it too and `finish` removes only their own
    branches (proposal 6.3). The spec Task is created in `todo`; `cmd_feature` queues it to start."""
    main = s.loc["folder"]
    if s.loc["kind"] != "repo":
        raise Fail("A Feature needs a git repo; this folder isn't one.")
    title = (title or "").strip()
    if not title:
        raise Fail("A Feature needs a title.")
    mode = mode or detect_mode(main)
    if mode not in ("remote", "local"):
        raise Fail(f"mode is remote or local, not {mode!r}.")
    change = (change or change_name(title)).strip()
    base = base_ref(main, (base or current_base(main)).strip())

    feature = s.new_feature(title, change, branch="", base=base, mode=mode, auto_fanout=auto_fanout)
    slug = slug_title(title)
    fb = f"feature/{s.prefix}-{feature['id']}-{slug}"
    if git(main, "show-ref", "--verify", "--quiet", f"refs/heads/{fb}")[0]:
        raise Fail(f"Branch {fb} already exists; pick another title or remove it first.")
    ok, _, err = git(main, "branch", fb, base)
    if not ok:
        raise Fail(err or f"Couldn't create the feature branch {fb}.")
    feature["branch"] = fb

    spec_title = f"Plan {title}"
    spec = s.new(spec_title, _spec_prompt(title, change), "worktree",
                 feature=feature["id"], role="spec", owns=[], risk="low")
    spec["base"] = fb
    spec["base_set"] = True
    spec["branch"] = f"task/{s.prefix}-{feature['id']}-{s.prefix}-{spec['id']}-{slug_title(spec_title)}"
    feature["spec_task"] = spec["id"]
    s.dirty = True
    return feature


# ---- the Plan approval hook and the fan-out trigger read ------------------------------------

def on_spec_accepted(s, task):
    """The Plan approval hook (C7, C8): `commands.cmd_accept` calls it after it accepts a spec Task.
    It records Plan approval and moves the Feature from `planning` to `building`. It does not fan
    out: the trigger is in `tick.py` and waits for `spec_merged` (C8), because Accept can return
    while the spec Task's merge is still pending (checks) or only requested (local)."""
    if task.get("role") != "spec" or not task.get("feature"):
        return
    feature = s.feature_by_id(task["feature"])
    if not feature or feature.get("phase") != "planning":
        return
    _set_phase(feature, "building")
    feature["plan_approved_at"] = now()
    s.dirty = True


def spec_merged(s, feature):
    """True when the spec Task has merged and the approved work table is on the feature branch; the
    fan-out trigger in `tick.py` reads it (C7, C8)."""
    spec = s.task_by_id(feature.get("spec_task")) if feature.get("spec_task") else None
    if not spec or not (spec.get("merged") or spec.get("state") == "done"):
        return False
    path = f"openspec/changes/{feature.get('change')}/tasks.md"
    return git(s.loc["folder"], "cat-file", "-e", f"{feature['branch']}:{path}")[0]


# ---- show -----------------------------------------------------------------------------------

def _task_state(task):
    return task.get("state") if task else "gone"


def show(s, feature):
    """The full state of a Feature (C5, C7): the record (with its ref), each child's state, role,
    rows, risk, machine and digest, and the open verify findings (the current Verify Task's
    `findings`). A child on another machine is read from its digest (its Task is not in this
    folder's store); a child on this machine is read from its Task."""
    children = []
    digests = feature.get("digests") or {}
    for c in feature.get("children") or []:
        cid, machine = c.get("ref"), c.get("machine")
        d = digests.get(digest.child_key(c))
        if machine is None:
            t = s.task_by_id(cid)
            state = (d or {}).get("state") or _task_state(t)
            risk = (t or {}).get("risk", "low")
            role = (t or {}).get("role") or c.get("role")
        else:
            state = (d or {}).get("state") or "running"
            risk = "low"
            role = c.get("role")
        children.append({"ref": f"{s.prefix}-{cid}", "state": state, "role": role,
                         "rows": list(c.get("rows") or []), "risk": risk,
                         "machine": machine, "digest": d})

    findings = []
    if feature.get("verify_task"):
        vt = s.task_by_id(feature["verify_task"])
        findings = list((vt or {}).get("findings") or [])

    return {"feature": {**feature, "ref": s.ref(feature)}, "ref": s.ref(feature),
            "children": children, "findings": findings}


# ---- sync (merge the base branch into the feature branch early) ------------------------------

def _base_point(s, feature):
    """What `sync` merges into the feature branch: in remote mode the remote's base (fetched), so
    the feature branch catches up with work merged on GitHub; else the local base branch. A fetch
    that fails in remote mode is raised, not swallowed: merging a stale remote-tracking branch would
    report a clean sync that never saw the current base."""
    main, base = s.loc["folder"], feature.get("base") or "main"
    if feature.get("mode") == "remote":
        remote = remote_of(main)
        if remote in git(main, "remote")[1].split():
            ok, _, err = git_quick(main, "fetch", "--quiet", "--no-tags", remote, base)
            if not ok:
                raise Fail(f"Couldn't fetch {base} from {remote}: {err or 'the remote is unreachable'}. "
                           f"Sync again when it is reachable.")
            tracked = f"{remote}/{base}"
            if git(main, "show-ref", "--verify", "--quiet", f"refs/remotes/{tracked}")[0]:
                return tracked
    return base


def sync(s, feature):
    """Merge the base branch into the feature branch early, to keep the final merge small (C7). It
    works in a throwaway worktree on the feature branch, so the folder's own checkout never moves.
    A conflict aborts the merge and leaves the feature branch untouched (`deletes nothing`)."""
    main, fb = s.loc["folder"], feature["branch"]
    base_point = _base_point(s, feature)
    tmp = tempfile.mkdtemp(prefix="herdr-feature-sync-")
    try:
        ok, _, err = git(main, "worktree", "add", "--quiet", tmp, fb)
        if not ok:
            raise Fail(err or f"Couldn't check out {fb} to merge the base in.")
        try:
            before = git(tmp, "rev-parse", "HEAD")[1]
            merged, _, _ = git(tmp, "merge", "--no-edit", base_point)
            if not merged:
                git(tmp, "merge", "--abort")
                return {"ok": True, "merged": False, "conflict": True}
            after = git(tmp, "rev-parse", "HEAD")[1]
            return {"ok": True, "merged": after != before, "conflict": False}
        finally:
            git(main, "worktree", "remove", "--force", tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- cancel ---------------------------------------------------------------------------------

def _feature_tasks(s, feature):
    """Every Task of this Feature in this folder's store (the spec Task, its children, Verify and
    Fix Tasks). A child on another machine is not here; cancel stops only this machine's Tasks."""
    return [t for t in s.data.get("tasks") or [] if t.get("feature") == feature["id"]]


def cancel(s, feature):
    """Stop the Feature's running Tasks and remove its queued ones; keep the feature branch and
    delete nothing (C7, proposal 6.12). A running Task's agents are killed and it is marked
    cancelled; a queued, scheduled or not-started Task is taken out of the queue and marked
    cancelled. No branch, worktree or file is removed.

    This stops the Tasks in this (home) machine's store and sets the Feature to `cancelled`. A child
    that runs on another machine is not in this store; `tick.py` (T19), which already polls each
    machine each tick, stops it when it sees the cancelled phase. feature.py may change only its own
    file and must not run herdr-task on other machines, so cross-machine stops are not done here."""
    stopped, removed = [], []
    for t in _feature_tasks(s, feature):
        if t.get("state") in ("done", "cancelled"):
            continue
        live = live_agents(s.key(t))
        if t.get("state") == "running" or live:
            for k in live:
                run(["hc", "kill", k["address"]])
            t["state"], t["cancelled"] = "cancelled", True
            stopped.append(s.ref(t))
        else:
            for k in ("queue", "queued_at", "scheduled_for", "held"):
                t.pop(k, None)
            t["state"], t["cancelled"] = "cancelled", True
            removed.append(s.ref(t))
    _set_phase(feature, "cancelled")
    feature["attention"] = ""
    s.dirty = True
    return {"ok": True, "stopped": stopped, "removed": removed}


# ---- merge (the person's Merge feature action) ----------------------------------------------

def _squash_merge_local(main, fb, base, message):
    """Squash-merge the feature branch into the base branch locally (local mode, proposal 6.10),
    as the classic local merge does. Returns an error string, or None on success.

    The person's work is never touched or lost: when the base is the folder's own checkout the merge
    runs there only if that checkout is clean (else it refuses and the person keeps their edits), so
    the `reset --hard` that undoes a conflicted squash can only restore the base tip, not discard
    uncommitted work. Otherwise the merge runs in a throwaway worktree on the base."""
    own_tmp = None
    if current_base(main) == base:
        if git(main, "status", "--porcelain")[1].strip():
            return (f"The folder has uncommitted changes on {base}; commit or set them aside, then "
                    f"merge again. Nothing was changed.")
        wt = main
    else:
        own_tmp = tempfile.mkdtemp(prefix="herdr-feature-merge-")
        ok, _, err = git(main, "worktree", "add", "--quiet", own_tmp, base)
        if not ok:
            shutil.rmtree(own_tmp, ignore_errors=True)
            return err or f"Couldn't check out {base} to merge into."
        wt = own_tmp
    try:
        ok, _, err = git(wt, "merge", "--squash", fb)
        if not ok:
            # The worktree was clean before, so this restores it to the base tip and loses nothing.
            git(wt, "reset", "--hard", "HEAD")
            return err or f"Couldn't squash-merge {fb} into {base} (conflicts)."
        ok, out, err = git(wt, "commit", "-m", message)
        if not ok and "nothing to commit" not in (out + err):
            git(wt, "reset", "--hard", "HEAD")
            return err or f"Couldn't commit the merge of {fb}."
        return None
    finally:
        if own_tmp:
            git(main, "worktree", "remove", "--force", own_tmp)
            shutil.rmtree(own_tmp, ignore_errors=True)


def merge(s, feature):
    """The person's Merge feature action, only in phase `ready` (C4, C7). In remote mode it
    squash-merges the open pull request into the base; in local mode it squash-merges the feature
    branch locally. Then it moves the Feature to `done`."""
    if feature.get("phase") != "ready":
        raise Fail(f"{s.ref(feature)} isn't ready to merge (phase {feature.get('phase')}); "
                   f"Merge feature works only in phase ready.")
    pr = feature.get("pr") or {}
    url = pr.get("url")
    if feature.get("mode") == "remote":
        # Remote mode merges the pull request, never a silent local squash: that would report the
        # Feature done without landing anything on GitHub.
        if not pr.get("number"):
            raise Fail(f"{s.ref(feature)} has no open pull request to merge; closing opens it. "
                       f"Try Merge feature again once the pull request is open.")
        repo = pr.get("repo") or github(s)
        if not repo:
            raise Fail("No GitHub repo for this Feature's pull request.")
        ok, _, err = gh("pr", "merge", str(pr["number"]), "--repo", repo, "--squash")
        if not ok:
            raise Fail(f"Couldn't merge the pull request: {err}")
    else:
        err = _squash_merge_local(s.loc["folder"], feature["branch"], feature.get("base") or "main",
                                  f"{s.ref(feature)} {feature.get('title')}")
        if err:
            raise Fail(err)
    _set_phase(feature, "done")
    feature["attention"], feature["done_at"] = "", now()
    s.dirty = True
    return {"ok": True, "pr": url, "phase": "done"}


# ---- fix round (the person's Add a fix round action) ----------------------------------------

def fix_round(s, feature):
    """The person's Add a fix round action (C4, C7). It is offered when the Feature shows Needs you
    from open verify findings after the allowed fix rounds. It adds one more fix round and a new
    Verify Task, clears the Needs-you, and moves the Feature back to `verifying`. The Assistant adds
    the Fix Tasks for the findings; the new Verify Task waits for them."""
    if feature.get("attention") != VERIFY_OPEN:
        raise Fail(f"{s.ref(feature)} is not waiting for a fix round "
                   f"(it shows {feature.get('attention') or 'nothing'}, not {VERIFY_OPEN}).")
    feature["fix_rounds"] = int(feature.get("fix_rounds") or 0) + 1
    title = feature.get("title")
    verify = s.new(f"Verify {title}", f"Run /opsx:verify {feature.get('change')} on the feature "
                   f"branch and report with herdr-task verify-report.", "worktree",
                   feature=feature["id"], role="verify", owns=[], risk="low")
    verify["base"] = feature["branch"]
    verify["base_set"] = True
    verify["branch"] = f"task/{s.prefix}-{feature['id']}-{s.prefix}-{verify['id']}-verify"
    feature["verify_task"] = verify["id"]
    _set_phase(feature, "verifying")
    feature["attention"] = ""
    s.dirty = True
    return {"ok": True, "fix_rounds": feature["fix_rounds"], "verify_task": s.ref(verify)}


# ---- get ------------------------------------------------------------------------------------

def get(s, ref):
    """The Feature a ref names; raises `Fail` when the ref is a Task or missing (C7)."""
    return s.find_feature(ref)


# ---- the `feature` command ------------------------------------------------------------------

def _ensure_scheduler_quietly():
    try:
        ensure_scheduler(quiet=True)
    except Exception:
        pass


def _queue_spec(s, spec):
    """Queue the spec Task so the scheduler starts it: `feature add` starts planning by itself, as
    the Assistant template says (the person does not run a separate start). The queue holds its
    agent argv and the caller's space; the scheduler begins it when there is room."""
    space = caller_space()
    spec["state"], spec["queued_at"] = "queued", now()
    spec["queue"] = {"argv": worker_argv(s, spec, space), "space": space}
    s.dirty = True


def _cmd_add(args):
    with open_store(args) as s:
        if s.loc["kind"] != "repo":
            raise Fail("A Feature needs a git repo; this folder isn't one.")
        mode = args.mode or detect_mode(s.loc["folder"])
        feature = create(s, args.title, base=args.base, change=args.change, mode=mode,
                         auto_fanout=getattr(args, "auto_fanout", True))
        _queue_spec(s, s.task_by_id(feature["spec_task"]))
        out = {**feature, "ref": s.ref(feature)}
        ref = s.ref(feature)
    _ensure_scheduler_quietly()
    warn = "No GitHub remote: local mode, one machine only" if mode == "local" else None
    if warn:
        # First, as the contract says. With --json the record is the only thing on stdout.
        print(warn, file=(sys.stderr if getattr(args, "json", False) else sys.stdout))
    say(args, out, f"{ref}: {out['title']} created ({feature['phase']}); its spec Task is "
                   f"{s.ref(s.task_by_id(feature['spec_task']))}.")


def _cmd_list(args):
    with open_store(args, write=False) as s:
        features = [{**f, "ref": s.ref(f)} for f in s.data.get("features") or []]
    text = "\n".join(f"{f['ref']:<9} {f['phase']:<10} {f['title']}" for f in features) or "No features here."
    say(args, {"features": features}, text)


def _cmd_show(args):
    # `feature show <ref> --on <machine>` reads a Feature whose home is another machine (C4). The
    # Feature record lives on its home machine, so the show is forwarded there, the same way the
    # top-level `herdr-task --on <machine> ...` forwards; `--on` for this machine reads locally.
    on = getattr(args, "on", None)
    if on and on != machine_name():
        from . import cli
        return cli.remote(on, ["feature", "show", args.ref] + (["--json"] if getattr(args, "json", False) else []))
    with open_store(args, write=False) as s:
        feature = get(s, args.ref)
        data = show(s, feature)
    text = [f"{data['ref']}  {feature['title']}  ({feature['phase']}"
            + (f", needs you: {feature['attention']}" if feature.get("attention") else "") + ")"]
    for c in data["children"]:
        text.append(f"  {c['ref']:<9} {c['state']:<10} {c['role'] or ''} {','.join(c['rows'])}"
                    + (f" [{c['machine']}]" if c['machine'] else ""))
    if data["findings"]:
        text.append(f"  open findings: {len(data['findings'])}")
    say(args, data, "\n".join(text))


def _cmd_sync(args):
    with open_store(args) as s:
        feature = get(s, args.ref)
        data = sync(s, feature)
    text = ("merged the base in" if data["merged"] else
            "conflict: the base did not merge, an Integration Task is needed" if data["conflict"] else
            "already current")
    say(args, data, f"{s.ref(feature)}: {text}.")


def _cmd_cancel(args):
    with open_store(args) as s:
        feature = get(s, args.ref)
        data = cancel(s, feature)
        ref = s.ref(feature)
    bits = []
    if data["stopped"]:
        bits.append("stopped " + ", ".join(data["stopped"]))
    if data["removed"]:
        bits.append("removed " + ", ".join(data["removed"]))
    say(args, data, f"{ref} cancelled" + (": " + "; ".join(bits) if bits else "")
                    + ". The feature branch is kept; nothing was deleted.")


def _cmd_merge(args):
    with open_store(args) as s:
        feature = get(s, args.ref)
        data = merge(s, feature)
        ref = s.ref(feature)
    say(args, data, f"{ref} merged into {feature.get('base') or 'main'}; done.")


def _cmd_fix_round(args):
    with open_store(args) as s:
        feature = get(s, args.ref)
        data = fix_round(s, feature)
        ref = s.ref(feature)
    say(args, data, f"{ref}: fix round {data['fix_rounds']} added, new Verify Task {data['verify_task']}.")


_SUBCOMMANDS = {
    "add": _cmd_add,
    "list": _cmd_list,
    "show": _cmd_show,
    "sync": _cmd_sync,
    "cancel": _cmd_cancel,
    "merge": _cmd_merge,
    "fix-round": _cmd_fix_round,
}


def cmd_feature(args):
    """The `feature` command entry; dispatches on `args.feature_cmd` (C4, C7)."""
    cmd = getattr(args, "feature_cmd", None)
    handler = _SUBCOMMANDS.get(cmd)
    if not handler:
        raise Fail("Use: feature add|show|list|sync|cancel|merge|fix-round")
    return handler(args)
