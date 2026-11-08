"""`flow init`: prepare a repo for the OpenSpec flow (proposal section 6.1).

It runs `openspec init`, looks for each Matt Pocock skill at repo level (`.claude/skills`) and at
user level (`~/.claude/skills`) on the machine where the worker runs, asks the person only for a
skill that is missing at both levels, copies an accepted skill into the repo pinned to a source
commit (keeping the MIT notice), and commits the result as an ordinary pull request. `--update`
moves the pinned copies to a newer commit.
"""

import json
import os
import shutil
import sys
import tempfile

from . import base
from .base import Fail
from .store import locate


# The six Matt Pocock skills the flow uses (proposal section 6.1). Their folders are scattered
# across the repo's categories (grill-with-docs, domain-modeling, to-spec and retro under
# skills/engineering/; grilling and writing-for-agents under skills/productivity/), so a copy
# finds a skill by its folder name, not by a fixed path.
SKILLS = ["grill-with-docs", "grilling", "domain-modeling", "to-spec", "retro", "writing-for-agents"]
MATT_POCOCK_REPO = "https://github.com/mattpocock/skills"
# The pinned source commit. `copy_skill` writes it into each copy; `flow init --update` moves a
# pinned copy to this value, and leaves a copy already at it as it is.
MATT_POCOCK_COMMIT = "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d"

# A copy that flow init made carries this marker (the source repo and commit), so `--update` can
# tell a pinned copy apart from a skill the person wrote by hand and never moves the latter.
SOURCE_MARKER = ".source.json"

# The branch flow init commits on, so the person reviews the change as an ordinary pull request.
INIT_BRANCH = "chore/flow-init"
UPDATE_BRANCH = "chore/flow-init-update"

_REPO_CACHE = {}


# ---- skill locations ------------------------------------------------------------------------

def _repo_skills(folder):
    return os.path.join(folder, ".claude", "skills")


def USER_SKILLS_DIR():
    # A function, not a constant, so a test's patched base.HOME is honoured.
    return os.path.join(base.HOME, ".claude", "skills")


def skill_present(name, folder, machine=None):
    """`"repo"` when the skill is in the repo's `.claude/skills`, `"user"` when it is in
    `~/.claude/skills`, else `None`. On another machine (`machine`), the same two tests run
    there over `hc` (mocked in the tests)."""
    if machine:
        for level, root in (("repo", os.path.join(folder, ".claude", "skills", name)),
                            ("user", os.path.join("$HOME", ".claude", "skills", name))):
            r = base.run(["hc", "exec", "--on", machine, "--", "sh", "-c", f'test -d "{root}"'])
            if r.returncode == 0:
                return level
        return None
    if os.path.isdir(os.path.join(_repo_skills(folder), name)):
        return "repo"
    if os.path.isdir(os.path.join(USER_SKILLS_DIR(), name)):
        return "user"
    return None


def _pinned(folder, name):
    """True when a skill in the repo is a copy flow init made (it carries the source marker)."""
    return os.path.isfile(os.path.join(_repo_skills(folder), name, SOURCE_MARKER))


def _pinned_commit(folder, name):
    """The commit a pinned repo copy records, or `None` when there is no readable marker."""
    try:
        with open(os.path.join(_repo_skills(folder), name, SOURCE_MARKER)) as f:
            return json.load(f).get("commit")
    except (OSError, ValueError):
        return None


# ---- the source repo and the copy -----------------------------------------------------------

def _repo_at(commit):
    """A local checkout of the Matt Pocock skills repo at `commit`. It clones once per commit and
    keeps the path; the tests patch it, so the network is never reached there."""
    cached = _REPO_CACHE.get(commit)
    if cached and os.path.isdir(cached):
        return cached
    dst = tempfile.mkdtemp(prefix="herdr-mp-skills-")
    r = base.run(["git", "clone", "--quiet", MATT_POCOCK_REPO, dst])
    if r.returncode != 0:
        raise Fail(f"Could not fetch {MATT_POCOCK_REPO}: {(r.stderr or r.stdout).strip() or 'git clone failed'}")
    ok, _, err = base.git(dst, "checkout", "--quiet", commit)
    if not ok:
        raise Fail(f"Could not check out {commit} of {MATT_POCOCK_REPO}: {err or 'checkout failed'}")
    _REPO_CACHE[commit] = dst
    return dst


def _find_skill_dir(repo, name):
    """The skill's folder in the source repo (a directory named `name` with a `SKILL.md`)."""
    root = os.path.join(repo, "skills")
    start = root if os.path.isdir(root) else repo
    for here, _dirs, files in os.walk(start):
        if os.path.basename(here) == name and "SKILL.md" in files:
            return here
    return None


def copy_skill(name, folder, commit):
    """Copy a skill from the Matt Pocock repo into the repo's `.claude/skills/<name>`, pinned to
    `commit` and carrying the MIT notice. Replaces an older copy of the same skill."""
    repo = _repo_at(commit)
    src = _find_skill_dir(repo, name)
    if not src:
        raise Fail(f"The skill {name!r} is not in {MATT_POCOCK_REPO} at {commit}.")
    dst = os.path.join(_repo_skills(folder), name)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(src, dst)
    # Keep the MIT notice: the repo is MIT, so its LICENSE goes beside the copied skill.
    license_file = os.path.join(repo, "LICENSE")
    if os.path.isfile(license_file):
        shutil.copy2(license_file, os.path.join(dst, "LICENSE"))
    # Pin the copy to the source commit, so `--update` knows what is here.
    with open(os.path.join(dst, SOURCE_MARKER), "w") as f:
        json.dump({"repo": MATT_POCOCK_REPO, "commit": commit, "name": name}, f, indent=2)
        f.write("\n")


# ---- the ask --------------------------------------------------------------------------------

def _confirm_tty(name):
    """The default ask for a skill missing at both levels: yes or no from the person. The CLI's
    chat card answers it where the worker runs with one; without an answer it is a no. The prompt
    goes to stderr, so a `--json` run still prints one clean JSON object to stdout."""
    prompt = (f"The skill {name!r} is not in this repo or in your ~/.claude/skills. "
              f"Add a pinned copy from {MATT_POCOCK_REPO}? [y/N] ")
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return input().strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---- the commit -----------------------------------------------------------------------------

def _commit_pr(folder, paths, update):
    """Commit exactly the paths flow init wrote, on its own fresh branch, so the person reviews it
    as an ordinary pull request. It leaves any other work in the tree or the index untouched, never
    resets an existing branch, and raises on a real git error (it does not report failure as
    success). Returns the branch name, or `None` when those paths hold no change to commit."""
    here = [p for p in paths if os.path.exists(os.path.join(folder, p))]
    if not here:
        return None
    ok, status, _ = base.git(folder, "status", "--porcelain", "--", *here)
    if not status.strip():
        return None

    branch = UPDATE_BRANCH if update else INIT_BRANCH
    # Never reset an existing branch (its unmerged history is the person's): refuse instead.
    exists, _, _ = base.git(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if exists:
        raise Fail(f"The branch {branch} already exists here. Delete or rename it, then run flow init again.")
    ok, _, err = base.git(folder, "checkout", "-b", branch)
    if not ok:
        raise Fail(f"Could not start the branch {branch}: {err or 'git checkout failed'}")

    # Stage and commit only these paths, so nothing already staged rides along (--only).
    ok, out, err = base.git(folder, "add", "--", *here)
    if not ok:
        raise Fail(f"Could not stage the flow init changes: {(err or out).strip() or 'git add failed'}")
    message = "Update the pinned OpenSpec flow skills" if update else "Set up the OpenSpec flow"
    ok, out, err = base.git(folder, "-c", "commit.gpgsign=false", "commit", "--only", "-m", message, "--", *here)
    if not ok:
        raise Fail(f"Could not commit the flow init changes: {(err or out).strip() or 'git commit failed'}")
    return branch


# ---- openspec -------------------------------------------------------------------------------

def _openspec_init(folder):
    """Run `openspec init` in the repo. The tests patch it, so OpenSpec is never run there."""
    r = base.run(["openspec", "init"], cwd=folder)
    if r.returncode != 0:
        raise Fail(f"openspec init failed: {(r.stderr or r.stdout).strip() or 'see openspec'}")


# ---- the command ----------------------------------------------------------------------------

def init(folder, update=False, confirm=None):
    """Prepare `folder` for the OpenSpec flow (proposal section 6.1). Returns the report of C5:
    `{"ok", "openspec": "initialized|present", "skills": {name: "repo|user|added|skipped"}, "pr"}`.

    `confirm(name) -> bool` is the ask for a skill missing at both levels; it is called only in
    that case, and never in `--update` (which moves pinned copies, it does not add new skills)."""
    folder = os.path.realpath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise Fail(f"No such folder: {folder}")
    if confirm is None:
        confirm = _confirm_tty
    commit = MATT_POCOCK_COMMIT
    report = {"ok": True, "openspec": "", "skills": {}, "pr": None}
    touched = []  # the exact paths flow init wrote, for a precise commit

    if os.path.isdir(os.path.join(folder, "openspec")):
        report["openspec"] = "present"
    else:
        _openspec_init(folder)
        report["openspec"] = "initialized"
        touched.append("openspec")

    for name in SKILLS:
        where = skill_present(name, folder)
        skill_path = os.path.join(".claude", "skills", name)
        if where in ("repo", "user"):
            if update and where == "repo" and _pinned(folder, name) and _pinned_commit(folder, name) != commit:
                # Move the pinned copy to the target commit; a copy already there stays as it is.
                copy_skill(name, folder, commit)
                report["skills"][name] = "added"
                touched.append(skill_path)
            else:
                report["skills"][name] = where
            continue
        # Missing at both levels.
        if update:
            # `--update` only moves pinned copies; it does not add a skill the repo never had.
            report["skills"][name] = "skipped"
            continue
        if confirm(name):
            copy_skill(name, folder, commit)
            report["skills"][name] = "added"
            touched.append(skill_path)
        else:
            report["skills"][name] = "skipped"

    if touched:
        report["pr"] = _commit_pr(folder, touched, update)
    return report


def _human(report):
    lines = [f"openspec: {report['openspec']}"]
    for name, where in report["skills"].items():
        lines.append(f"  {name}: {where}")
    lines.append(f"pull request branch: {report['pr'] or 'none'}")
    return "\n".join(lines)


def cmd_flow_init(args):
    """The `flow init [--update]` command entry."""
    folder = locate(getattr(args, "folder", None))["folder"]
    report = init(folder, update=getattr(args, "update", False))
    if getattr(args, "json", False):
        print(json.dumps(report))
    else:
        print(_human(report))
