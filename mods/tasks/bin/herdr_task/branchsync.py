"""sync-branch: a child worker keeps its task branch current with its feature branch (proposal
section 6.9, rules 4 to 8). A worker runs it in its own worktree before `ready` and when the
feature branch moves. It does the work with git only, never with the model's judgment: a backup
ref and rerere, a rebase onto the feature branch, a clean abort and restore on a conflict, the
lockfiles reported for regeneration, and the project checks after a clean rebase.

The owned-paths precheck (proposal section 6.8) reuses ready_guards: `changed_paths` for the
worker's own diff against the base and `owned_paths_check` to judge it, the same check `ready` runs.
It refuses with `owned-paths` before touching the branch when a changed path is outside the owned
paths. A dirty worktree is refused up front so nothing it holds can be lost."""

import json
import os
import shlex
import sys

from . import ready_guards
from .base import Fail, git, now, run
from .settings import settings_for
from .store import open_store


# The reason codes are the contract (callers branch on the reason, not the text), and each is the
# command's exit code (docs/openspec-flow.md C6).
CLEAN = 0
OWNED_PATHS = 2
CONFLICT = 3
CHECKS_FAILED = 4

CLEAN_REASON = "clean"
OWNED_PATHS_REASON = "owned-paths"
CONFLICT_REASON = "conflict"
CHECKS_FAILED_REASON = "checks-failed"

CODE_OF = {
    CLEAN_REASON: CLEAN,
    OWNED_PATHS_REASON: OWNED_PATHS,
    CONFLICT_REASON: CONFLICT,
    CHECKS_FAILED_REASON: CHECKS_FAILED,
}

# A check command for `post_checks` (rule 8), overridable for tests and for repos without bin/check.
CHECK_ENV = "HERDR_TASK_CHECK"


def _result(reason, outside_owns=None, conflicted=None, regenerate=None, backup_ref="", detail=""):
    return {
        "reason": reason,
        "outside_owns": list(outside_owns or []),
        "conflicted": list(conflicted or []),
        "regenerate": list(regenerate or []),
        "backup_ref": backup_ref,
        "detail": detail,
    }


def _current_branch(folder):
    ok, name, _ = git(folder, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok or not name or name == "HEAD":
        raise Fail("sync-branch: not on a branch (detached HEAD). Check out your task branch first.")
    return name


def _dirty(folder):
    """True when the worktree has uncommitted tracked changes. A rebase would refuse to start, so
    sync-branch refuses first and never runs a reset that could discard them."""
    ok, out, _ = git(folder, "status", "--porcelain", "--untracked-files=no")
    return bool(out.strip()) if ok else False


def _rebase_in_progress(folder):
    """True while a rebase is mid-flight (a real conflict), false when a rebase never started."""
    for d in ("rebase-merge", "rebase-apply"):
        ok, path, _ = git(folder, "rev-parse", "--git-path", d)
        if ok and os.path.exists(path if os.path.isabs(path) else os.path.join(folder, path)):
            return True
    return False


def _conflicted(folder):
    """The unmerged files during a conflicted rebase, NUL-separated so special characters are not
    quoted. Reads raw stdout (not base.git, which strips, losing a leading-space filename)."""
    r = run(["git", "-C", folder, "diff", "--name-only", "--diff-filter=U", "-z"])
    return [p for p in r.stdout.split("\0") if p] if r.returncode == 0 else []


def _split_conflicts(conflicted, owns, exempt=None):
    """A conflicted file is one of three kinds: outside the owned paths (the worker must ask the
    Assistant to extend them), a lockfile or generated file to regenerate (never merged by hand,
    rule 7), or an owned file the worker resolves. Returns (outside_owns, regenerate).

    owned_paths_check always folds in ready_guards.DEFAULT_EXEMPT (plus the space's owns_exempt), so a
    check with empty owns leaves only the exempt files inside, and those are the ones to regenerate."""
    outside = ready_guards.owned_paths_check(conflicted, owns, exempt)["outside"]
    non_exempt = set(ready_guards.owned_paths_check(conflicted, [], exempt)["outside"])
    regenerate = [p for p in conflicted if p not in non_exempt]
    return outside, regenerate


def backup_ref(folder, branch):
    """Rule 4: a backup ref `backup/<branch>-<time>` at the current HEAD, before the rebase, so an
    abort can always restore the branch. Returns the ref name."""
    name = f"backup/{branch}-{now()}"
    ok, _, err = git(folder, "branch", "--force", name, "HEAD")
    if not ok:
        raise Fail(f"sync-branch: could not make the backup ref {name}: {err}")
    return name


def enable_rerere(folder):
    """Rule 4: turn on rerere so a resolution the worker made once is replayed on the next sync."""
    git(folder, "config", "rerere.enabled", "true")


# Conventional check-script locations, tried in order after the HERDR_TASK_CHECK override. The
# contract does not name the project's check command, so a repo points at it with HERDR_TASK_CHECK
# (what Shepherdr's own flow sets) or one of these scripts.
CHECK_SCRIPTS = ("bin/check", "scripts/check", "check")


def _check_command(folder):
    """The project's check command, or None when the repo defines none (nothing to run)."""
    cmd = os.environ.get(CHECK_ENV)
    if cmd:
        return shlex.split(cmd)
    for rel in CHECK_SCRIPTS:
        p = os.path.join(folder, rel)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return [p]
    return None


def post_checks(folder, paths):
    """Rule 8: run the project checks for the changed paths after a clean rebase. The command is the
    HERDR_TASK_CHECK override or a conventional check script (see CHECK_SCRIPTS); the changed paths
    are passed as arguments. A repo that defines no check command has nothing to run, which is not a
    failure (`ok`), and the detail says so. Returns {"ok": bool, "detail": str}."""
    argv = _check_command(folder)
    if argv is None:
        return {"ok": True, "detail": "no project check command configured"}
    r = run(argv + list(paths), cwd=folder)
    if r.returncode == 0:
        return {"ok": True, "detail": ""}
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return {"ok": False, "detail": (lines[-1][:200] if lines else "checks failed")}


def sync(folder, feature_branch, owns, mode, checks=True, exempt=None):
    """Rules 4 to 8 for remote and local mode, with the owned-paths precheck of section 6.8 first.

    folder: the worker's worktree. feature_branch: the branch to rebase onto (the Task's base).
    owns: the worker's owned paths. mode: "remote" or "local". checks: run post_checks (rule 8).
    exempt: the space's owns_exempt globs, added to ready_guards.DEFAULT_EXEMPT for the owned-paths
    check (C6).

    Returns {"reason", "outside_owns", "conflicted", "regenerate", "backup_ref", "detail"} (C6)."""
    owns = list(owns or [])
    exempt = list(exempt or [])
    branch = _current_branch(folder)

    # Refuse a dirty worktree before anything, so no backup, rebase or reset touches uncommitted work.
    if _dirty(folder):
        raise Fail("sync-branch: you have uncommitted changes. Commit them or set them aside, then "
                   "run sync-branch again.")

    # Rule 5: remote mode fetches the feature branch; local mode rebases onto the local one.
    if mode == "remote":
        # An explicit destination refspec so the tracking ref is updated even in a single-branch
        # clone whose default fetch mapping would not cover this branch.
        dest = f"refs/remotes/origin/{feature_branch}"
        ok, _, err = git(folder, "fetch", "origin", f"+{feature_branch}:{dest}")
        if not ok:
            raise Fail(f"sync-branch: could not fetch {feature_branch}: {err}")
        target = dest
    else:
        target = feature_branch
    ok, _, _ = git(folder, "rev-parse", "--verify", "--quiet", target)
    if not ok:
        raise Fail(f"sync-branch: no such branch {target}.")

    # Section 6.8 precheck: the worker's own diff against the base, before touching the branch.
    # Refuse with `owned-paths` and rebase nothing when a changed path is outside the owned paths.
    changed = ready_guards.changed_paths(folder, target)
    check = ready_guards.owned_paths_check(changed, owns, exempt)
    if not check["ok"]:
        return _result(OWNED_PATHS_REASON, outside_owns=check["outside"])

    # Rule 4: the backup ref and rerere, before the rebase.
    bref = backup_ref(folder, branch)
    enable_rerere(folder)

    # Rule 5: rebase onto the feature branch.
    # - rebase.updateRefs is forced off: with it on (a repo or user config), git would move every
    #   branch pointing into the rebased range, including the backup branch just made, off the
    #   original tip and defeat the restore.
    # - --no-rerere-autoupdate keeps a conflict unmerged even when rerere replays a cached
    #   resolution; with autoupdate on, git would stage it and it would vanish from the conflict
    #   report (rules 6-7).
    ok, _, err = git(folder, "-c", "rebase.updateRefs=false", "rebase", "--no-rerere-autoupdate", target)
    if not ok:
        if not _rebase_in_progress(folder):
            # The rebase never started (not a conflict): the branch is untouched, backup kept.
            raise Fail(f"sync-branch: rebase could not start: {err or 'unknown error'}. "
                       f"Your branch is unchanged (backup {bref}).")
        # Rules 6 and 7: list the conflicts, mark the ones outside the owned paths and the lockfiles,
        # then abort, which restores the branch to where the backup ref also points.
        conflicted = _conflicted(folder)
        outside, regenerate = _split_conflicts(conflicted, owns, exempt)
        ok_ab, _, aberr = git(folder, "rebase", "--abort")
        if not ok_ab:
            raise Fail(f"sync-branch: conflict, and the rebase abort failed: {aberr or 'unknown error'}. "
                       f"Restore your branch from {bref}.")
        return _result(CONFLICT_REASON, outside_owns=outside, conflicted=conflicted,
                       regenerate=regenerate, backup_ref=bref)

    # Rule 8: the project checks for the changed paths, after a clean rebase.
    detail = ""
    if checks:
        res = post_checks(folder, ready_guards.changed_paths(folder, target))
        if not res["ok"]:
            return _result(CHECKS_FAILED_REASON, backup_ref=bref, detail=res["detail"])
        # Carry a note (e.g. no check command configured) so the clean message can show it.
        detail = res["detail"]

    # Clean: already current or a clean rebase. The diff is unchanged, so the review stays valid.
    return _result(CLEAN_REASON, backup_ref=bref, detail=detail)


# ---- the command entry ----------------------------------------------------------------------

def _message(res):
    reason = res["reason"]
    if reason == CLEAN_REASON:
        return "sync-branch: clean" + (f" ({res['detail']})" if res.get("detail") else "")
    if reason == OWNED_PATHS_REASON:
        return ("sync-branch refused: these changed paths are outside your owned paths: "
                f"{', '.join(res['outside_owns'])}. Ask the Assistant to extend them.")
    if reason == CONFLICT_REASON:
        return ("sync-branch: conflict, branch restored. "
                f"Conflicted: {', '.join(res['conflicted'])}. "
                f"Outside your owned paths: {', '.join(res['outside_owns'])}. "
                f"Regenerate: {', '.join(res['regenerate'])}.")
    if reason == CHECKS_FAILED_REASON:
        return f"sync-branch: rebased, but checks failed: {res['detail']}."
    return ""


def _detect_mode(folder):
    """The mode when the Feature record does not give one: remote with a GitHub remote, else local."""
    ok, url, _ = git(folder, "remote", "get-url", "origin")
    return "remote" if ok and "github.com" in (url or "") else "local"


def _current_task(s):
    """The running Task of this worktree: the one whose agent is this terminal, else the one whose
    worktree is here."""
    tasks = s.data.get("tasks") or []
    me = os.environ.get("HC_TERMINAL_ID")
    if me:
        for t in tasks:
            if any(a.get("address") == me for a in t.get("agents") or []):
                return t
    cwd = os.path.realpath(os.getcwd())
    for t in tasks:
        wt = t.get("worktree")
        if wt and os.path.realpath(wt) == cwd:
            return t
    return None


def cmd_sync_branch(args):
    """The `sync-branch` command: find this worktree's Task, sync its branch, print the envelope,
    return the exit code (C6)."""
    with open_store(args, write=False) as s:
        t = _current_task(s)
        if not t:
            raise Fail("sync-branch: no running task here. Run it from your task's worktree.")
        owns = t.get("owns") or []
        feat = None
        fid = t.get("feature")
        if fid is not None:
            feat = next((f for f in s.data.get("features") or [] if f.get("id") == fid), None)
        worktree = t.get("worktree") or s.loc["folder"]
        mode = (feat or {}).get("mode") or _detect_mode(worktree)
        base = getattr(args, "base", None) or t.get("base")
        if not base:
            raise Fail("sync-branch: no feature branch to sync onto (set --base).")
        # The space's extra exempt globs for the owned-paths check (C6), alongside DEFAULT_EXEMPT.
        exempt = (settings_for(s, t.get("space")).get("tasks") or {}).get("owns_exempt") or []
    res = sync(worktree, base, owns, mode, exempt=exempt)
    code = CODE_OF[res["reason"]]
    if getattr(args, "json", False):
        # The sync-branch --json shape (C6): the five keys, for every outcome.
        print(json.dumps({k: res[k] for k in ("reason", "conflicted", "outside_owns", "regenerate", "backup_ref")}))
    elif code == CLEAN:
        print(_message(res))
    else:
        # A refusal goes to stderr, like the other command refusals (C4).
        print(_message(res), file=sys.stderr)
    return code
