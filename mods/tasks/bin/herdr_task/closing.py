"""The closing steps of a Feature (proposal 6.11, contract C7 task T16).

Closing is the deterministic run after a clean verify (phase `closing`). It needs no agent and no
judgment from the model; `tick.py` calls `close` each tick while a Feature is in `closing`. In order
(proposal 6.11):

1. Tick every work-table row whose child merged, and make one commit on the feature branch. A row
   with no merged child leaves the Feature with `attention = "row-no-child"` (Needs you); closing
   stops there.
2. Run `openspec archive <change> -y` and commit the result on the feature branch.
3. Merge the base branch into the feature branch. On a conflict, abort the merge and create a risky
   Integration Task (base = the feature branch), and leave `attention = "base-conflict"`. The
   re-verify after that Task merges is the tick's job, not this module's.
4. Open the pull request into the base branch (remote mode) or prepare the local squash (local mode),
   and move the Feature to `ready`.

The glossary words are the proposal's (section 2): Feature, work table, row, closing, Integration
Task, base branch, feature branch. The attention reason codes (`row-no-child`, `base-conflict`) are
the contract's (C2).

Everything here shells out to git in a real folder, so the tests use a real throwaway repo; only `hc`
and `gh` (and, for the archive step, `openspec`) are mocked. The feature branch is not normally
checked out anywhere, so each step works in a short-lived worktree of it (reusing one that is already
checked out), which keeps whatever the main folder has checked out untouched.
"""

import os
import shutil
import tempfile

from .base import Fail, git, now, run
from .github import gh, github, remote_of
from . import worktable


# The archive tool. An env var lets a test point at a fake, the way `HERDR_TASK_GH` does for gh.
OPENSPEC = os.environ.get("HERDR_TASK_OPENSPEC") or "openspec"


# ---- the whole close (what tick.py calls) ---------------------------------------------------

def close(s, feature):
    """Run the closing steps of `feature` in order (proposal 6.11) and return a result dict.

    The steps share one worktree of the feature branch. `close` is idempotent: while an Integration
    Task it made is still open it waits (so a tick that fires again makes no duplicate), a row with
    no merged child or a base conflict stops it with the matching `attention`, and a clean run opens
    the pull request (or prepares the local squash) and moves the Feature to `ready`.
    """
    main = s.loc["folder"]
    result = {
        "ref": s.ref(feature), "ticked": [], "no_child": [], "archived": False,
        "conflict": False, "conflicted": [], "integration_task": None, "pr": None,
        "phase": feature.get("phase"), "attention": feature.get("attention", ""),
    }

    # Idempotent: a base conflict already made an Integration Task that has not merged yet. Wait for
    # it (the tick re-verifies once it merges); never make a second one.
    pending = _open_integration(s, feature)
    if pending is not None:
        result["integration_task"] = s.ref(pending)
        return result

    wt, created = _acquire(main, feature["branch"])
    try:
        change = feature["change"]
        # Resume safely. Closing can re-enter after it has already ticked and archived (a base
        # conflict, then its Integration Task merges and verify passes; or a push/PR that failed
        # after the archive commit). The archive step moved the live change folder, so on a re-entry
        # only the later steps are left: do not tick or archive again, just merge the base and open
        # the pull request.
        if _live_change(wt, change):
            ticked, no_child = _tick(s, feature, wt)
            result["ticked"], result["no_child"] = ticked, no_child
            if no_child:
                feature["attention"] = "row-no-child"
                s.dirty = True
                result["attention"] = feature["attention"]
                return result
            archive_change(wt, change)
            result["archived"] = True
        elif _archived_change(wt, change):
            result["archived"] = True
        else:
            raise Fail(f"no tasks.md for {change} on {feature['branch']} (neither live nor archived)")

        merged = merge_base_in(wt, feature)
        if merged.get("conflict"):
            task = make_integration_task(s, feature)
            feature["attention"] = "base-conflict"
            s.dirty = True
            result.update(conflict=True, conflicted=merged.get("conflicted", []),
                          integration_task=s.ref(task), attention="base-conflict")
            return result

        pr = _open_pr(s, feature, wt)
        result["pr"] = pr.get("pr")
        result["phase"] = feature.get("phase")
        result["attention"] = feature.get("attention", "")
        return result
    finally:
        _release(main, wt, created)


# ---- step 1: tick merged rows ---------------------------------------------------------------

def tick_merged_rows(s, feature):
    """Tick every work-table row whose child merged, commit once on the feature branch, and return
    the rows with no merged child (which set `attention = "row-no-child"`, done by the caller)."""
    main = s.loc["folder"]
    wt, created = _acquire(main, feature["branch"])
    try:
        return _tick(s, feature, wt)[1]
    finally:
        _release(main, wt, created)


def _change_dir(folder, change):
    return os.path.join(folder, "openspec", "changes", change)


def _live_change(folder, change):
    """The change is still under `openspec/changes/<change>` (not archived yet)."""
    return os.path.isfile(os.path.join(_change_dir(folder, change), "tasks.md"))


def _archived_change(folder, change):
    """The change has already been archived under `openspec/changes/archive/<change>`."""
    return os.path.isfile(os.path.join(folder, "openspec", "changes", "archive", change, "tasks.md"))


def _tick(s, feature, folder):
    """The tick work in a checked-out `folder`; returns `(ticked_rows, rows_with_no_merged_child)`."""
    change = feature["change"]
    path = os.path.join(_change_dir(folder, change), "tasks.md")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        raise Fail(f"no tasks.md for {change}: {path}")
    all_ids = worktable.checkbox_ids(text)
    merged = _merged_rows(s, feature)
    ticked, new_text = [], text
    for rid in all_ids:
        if rid in merged:
            new_text = worktable.tick(new_text, rid)
            ticked.append(rid)
    no_child = [rid for rid in all_ids if rid not in merged]
    if new_text != text:
        with open(path, "w") as f:
            f.write(new_text)
        git(folder, "add", os.path.join("openspec", "changes", change, "tasks.md"))
        _commit(folder, f"{s.ref(feature)}: tick merged rows")
    return ticked, no_child


def _merged_rows(s, feature):
    """The set of work-table row ids a merged child covers. A child's outcome comes from its digest
    (which `feature-status` carries from any machine), and, for a home-machine child with no digest
    yet, from the Task record."""
    rows = set()
    for child in feature.get("children") or []:
        if _child_state(s, feature, child) == "merged":
            rows.update(child.get("rows") or [])
    return rows


def _child_state(s, feature, child):
    """A child's end state (`merged`, `skipped`, `cancelled`, `failed`) or None if not ended yet."""
    key = f"{child.get('machine') or 'local'}:{child.get('ref')}"
    digest = (feature.get("digests") or {}).get(key)
    if digest and digest.get("state"):
        return digest["state"]
    if not child.get("machine"):
        task = s.task_by_id(child.get("ref"))
        if task is not None:
            return _local_state(task)
    return None


def _local_state(task):
    """A home-machine child's end state from its Task record (the same rule as digest.build)."""
    if task.get("skipped"):
        return "skipped"
    if task.get("cancelled") or task.get("state") == "cancelled":
        return "cancelled"
    if task.get("merged") or task.get("state") == "done":
        return "merged"
    return None


# ---- step 2: archive the change -------------------------------------------------------------

def archive_change(folder, change):
    """Run `openspec archive <change> -y` in `folder` and commit the result on the feature branch."""
    try:
        r = run([OPENSPEC, "archive", change, "-y"], cwd=folder)
    except FileNotFoundError:
        raise Fail("openspec is not installed here; cannot archive the change")
    if r.returncode != 0:
        raise Fail((r.stderr or r.stdout).strip() or f"openspec archive {change} failed")
    # Stage only the archive's own effect (everything lives under `openspec/`), never any unrelated
    # modified or untracked file that happens to sit in the worktree.
    git(folder, "add", "-A", "--", "openspec")
    _commit(folder, f"Archive {change}")


# ---- step 3: merge the base in --------------------------------------------------------------

def merge_base_in(folder, feature):
    """Merge the base branch into the feature branch in `folder`.

    Returns `{"conflict": False}` on a clean merge (an already-current branch included). On a
    conflict it lists the conflicted files, aborts the merge so the branch is left clean, and returns
    `{"conflict": True, "conflicted": [...]}` so the caller makes an Integration Task.
    """
    base = feature.get("base") or "main"
    target = base
    if feature.get("mode") == "remote":
        remote = remote_of(folder)
        # The whole point of closing is to integrate the real (remote) base. A failed fetch must not
        # fall back to a stale local base: that could merge clean and move the Feature to ready while
        # the remote base still conflicts. Stop instead; the next tick retries the fetch.
        ok, _, err = git(folder, "fetch", "--quiet", remote, base)
        if not ok:
            raise Fail(f"could not fetch {remote}/{base}: {err or 'fetch failed'}")
        target = f"{remote}/{base}"
    r = run(["git", "-C", folder, "merge", "--no-edit", target])
    if r.returncode == 0:
        return {"conflict": False}
    conflicted = _unmerged(folder)
    run(["git", "-C", folder, "merge", "--abort"])
    if not conflicted:
        # Not a conflict but some other failure (for example an unknown base ref): the abort above
        # cleared any half-merge, so surface it rather than pretend it was a conflict.
        raise Fail((r.stderr or r.stdout).strip() or f"could not merge {base}")
    return {"conflict": True, "conflicted": conflicted}


def _unmerged(folder):
    """The paths with merge conflicts, read NUL-separated so a special name is not quoted."""
    r = run(["git", "-C", folder, "diff", "--name-only", "--diff-filter=U", "-z"])
    return [name for name in r.stdout.split("\0") if name]


# ---- step 4: open the pull request ----------------------------------------------------------

def open_pr(s, feature):
    """Open the pull request into the base branch (remote mode) or prepare the local squash (local
    mode), and move the Feature to `ready`."""
    main = s.loc["folder"]
    wt, created = _acquire(main, feature["branch"])
    try:
        return _open_pr(s, feature, wt)
    finally:
        _release(main, wt, created)


def _open_pr(s, feature, folder):
    url = None
    if feature.get("mode") == "remote":
        repo = github(s)
        if not repo:
            raise Fail("remote mode but no GitHub repo; cannot open the pull request")
        remote = remote_of(s.loc["folder"])
        ok, _, err = git(folder, "push", "--force-with-lease", "-u", remote, feature["branch"])
        if not ok:
            raise Fail(f"could not push {feature['branch']}: {err}")
        existing = feature.get("pr") or {}
        if existing.get("number"):
            okv, state, _ = gh("pr", "view", str(existing["number"]), "--repo", repo,
                               "--json", "state", "-q", ".state")
            if okv and state == "OPEN":
                _ready(feature)
                s.dirty = True
                return {"pr": existing.get("url")}
        base = feature.get("base") or "main"
        body = (feature.get("title") or "").strip() or s.ref(feature)
        ok, out, err = gh("pr", "create", "--repo", repo, "--head", feature["branch"],
                          "--base", base, "--title", f"{s.ref(feature)} {feature['title']}",
                          "--body", body)
        if not ok or not out:
            raise Fail(f"could not open a pull request: {err}")
        url = out.split()[-1]
        feature["pr"] = {"number": int(url.rstrip("/").split("/")[-1]), "url": url, "repo": repo}
    _ready(feature)
    s.dirty = True
    return {"pr": url}


def _ready(feature):
    feature["phase"] = "ready"
    feature["attention"] = ""


# ---- the Integration Task on a base conflict ------------------------------------------------

def make_integration_task(s, feature):
    """Create the risky Integration Task that resolves a base conflict (proposal 6.11, glossary).

    Its base is the feature branch, so it branches from and merges into the feature branch. It is
    always risky (`risk=high`, `merge=ask`): the person must accept it. It is created once; a second
    call while one is still open returns that one.
    """
    pending = _open_integration(s, feature)
    if pending is not None:
        return pending
    ref = s.ref(feature)
    title = f"Integrate {ref} with its base"
    prompt = (
        f"Resolve the conflict between the feature branch {feature['branch']} and its base "
        f"{feature.get('base') or 'main'}. Work on your own branch, which branches from and merges "
        f"into the feature branch. Resolve every conflict, run the checks, then say ready."
    )
    task = s.new(title, prompt, "worktree", feature=feature["id"], role="integration",
                 risk="high", rows=[])
    task["base"] = feature["branch"]
    task["base_set"] = True
    task.setdefault("set", {})["merge"] = "ask"
    task["state"], task["queued_at"] = "queued", now()
    feature.setdefault("children", []).append({
        "ref": task["id"], "machine": None, "package": "integration",
        "rows": [], "role": "integration",
    })
    s.dirty = True
    return task


def _open_integration(s, feature):
    """An Integration Task of this Feature that has not finished yet, or None."""
    for child in feature.get("children") or []:
        if child.get("role") != "integration":
            continue
        task = s.task_by_id(child.get("ref"))
        if task is not None and task.get("state") != "done" and not task.get("skipped"):
            return task
    return None


# ---- worktree and commit helpers ------------------------------------------------------------

def _acquire(main, branch):
    """`(path, created)`: a worktree of `main` checked out on `branch`. Reuses one already checked
    out there; otherwise adds a short-lived worktree (removed by `_release`)."""
    existing = _worktree_of(main, branch)
    if existing and os.path.isdir(existing):
        # A worktree already checked out on the feature branch: reuse it, but never when it is dirty.
        # Closing commits with `git add`, so unrelated staged, modified or untracked work there would
        # land in a closing commit. Refuse instead and leave the person's work alone.
        if run(["git", "-C", existing, "status", "--porcelain"]).stdout.strip():
            raise Fail(f"the worktree for {branch} ({existing}) has uncommitted changes; "
                       f"closing will not commit them. Clean or commit them first.")
        return existing, False
    holder = tempfile.mkdtemp(prefix="herdr-closing-")
    path = os.path.join(holder, "wt")
    ok, _, err = git(main, "worktree", "add", "--quiet", path, branch)
    if not ok:
        shutil.rmtree(holder, ignore_errors=True)
        raise Fail(err or f"could not add a worktree for {branch}")
    return path, True


def _release(main, path, created):
    if not created:
        return
    git(main, "worktree", "remove", "--force", path)
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def _worktree_of(main, branch):
    """The path of a worktree of `main` that has `branch` checked out, or None."""
    ok, out, _ = git(main, "worktree", "list", "--porcelain")
    if not ok:
        return None
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch ") and path:
            if line[len("branch "):] == f"refs/heads/{branch}":
                return path
    return None


def _commit(folder, message):
    """Commit the staged changes in `folder`; do nothing when nothing is staged. Returns whether a
    commit was made."""
    if run(["git", "-C", folder, "diff", "--cached", "--quiet"]).returncode == 0:
        return False
    r = run(["git", "-C", folder, "commit", "-q", "-m", message])
    if r.returncode != 0:
        raise Fail((r.stderr or r.stdout).strip() or "git commit failed")
    return True
