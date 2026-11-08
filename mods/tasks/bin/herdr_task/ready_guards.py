"""Ready guards for the OpenSpec flow (T06).

Two deterministic checks that `herdr-task ready` and `herdr-task sync-branch` lean on for a child
Task that has owned paths (proposal sections 6.8 and 6.9). Neither depends on the judgment of the
model:

- The owned-paths check compares the diff against the base branch. A changed path that is not one of
  the Task's owned paths, and is not on the exempt list (lockfiles and generated files, which a repo
  can extend with `tasks.owns_exempt`), means `ready` is refused and the path is listed.
- Review validity uses the git patch-id of the whole diff against the base. `herdr-task reviewed`
  stores the patch-id next to the commit hash of each recorded round; `ready` accepts a final commit
  when a recorded round has the same commit hash or the same patch-id. A clean rebase keeps the
  diff the same, so its patch-id is unchanged and the review stays valid; conflict resolution, a
  regenerated file or a new commit changes the patch-id, so the worker then needs a fresh review.

The diff against the base is the three-dot `<base>...HEAD` diff (from the merge-base), so it holds
only the Task's own changes and stays the same when the base moves ahead by itself. The diff is read
with `--no-renames` (so a rename shows as both its removed source and its added destination: owning
the destination never hides a change to an unowned source) and with `-z` (so a path with a non-ASCII
or special character is not quoted and still matches its owned entry). `--no-renames` also pins the
patch-id so it does not depend on the machine's `diff.renames` setting. Everything here shells out to
git in a real folder; the tests use a real throwaway repo.
"""

import re
import subprocess

from .base import Fail, run


# The reason and exit codes of the contract (C6). Callers branch on these, not on the message text.
OWNED_PATHS = 2
REVIEW_STALE = 4

# Lockfiles and generated files that the owned-paths check always treats as inside, even when no
# `owns` glob covers them (C6). A repo adds to this list with the `tasks.owns_exempt` setting.
DEFAULT_EXEMPT = [
    "bun.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "web/generated/**",
    "**/*.generated.*",
]


# The wildcards an owns/exempt glob may use. A `[` or `]` is a literal path character, not a
# character class: the contract's globs are `*`, `**` and `?` only (C6), so a pattern with none of
# these is matched as a literal path or directory.
_GLOB_WILDCARDS = "*?"


def _glob_to_regex(pattern):
    """A compiled regex for one owns/exempt glob, with path-aware wildcards:

    - `**` matches any run of characters, across directory separators (and `**/` matches zero or
      more leading path segments).
    - `*` and `?` match within one path segment only (never a `/`).
    """
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                while i < n and pattern[i] == "*":
                    i += 1
                if i < n and pattern[i] == "/":
                    out.append("(?:.*/)?")  # `**/`: zero or more path segments
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(path, pattern):
    """True when a changed path is covered by one owns or exempt entry.

    A plain path with no wildcard matches itself, and, when it names a directory, every path under
    it (`src/parser` covers `src/parser/types.ts`). A glob is matched with `_glob_to_regex`.
    """
    if any(ch in pattern for ch in _GLOB_WILDCARDS):
        return _glob_to_regex(pattern).match(path) is not None
    prefix = pattern.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def owned_paths_check(changed, owns, exempt=None):
    """`{"ok": bool, "outside": [...]}` for a set of changed paths against a Task's owned paths.

    A path is inside when it matches one of `owns` or one of the exempt list (`DEFAULT_EXEMPT` plus
    the caller's `exempt`, normally `tasks.owns_exempt`). `outside` keeps the input order and holds
    every path that matches neither; `ok` is true only when `outside` is empty.
    """
    patterns = list(owns or []) + list(DEFAULT_EXEMPT) + list(exempt or [])
    outside = [p for p in changed if not any(_matches(p, pat) for pat in patterns)]
    return {"ok": not outside, "outside": outside}


def changed_paths(folder, base):
    """The names the three-dot diff against `base` changes.

    Read with `--no-renames` (a rename counts as both its removed source and its added destination)
    and `-z` (NUL-separated, so a path with a special or non-ASCII character is not quoted).
    """
    r = run(["git", "-C", folder, "diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD"])
    if r.returncode != 0:
        raise Fail((r.stderr or r.stdout).strip() or f"git diff against {base} failed")
    return [name for name in r.stdout.split("\0") if name]


def patch_id(folder, base):
    """The git patch-id of the whole three-dot diff against `base`, or `""` for an empty diff.

    The patch-id ignores commit hashes and (with `--stable`) the order of files, so a clean rebase
    that keeps the changed lines keeps the patch-id.
    """
    r = run(["git", "-C", folder, "diff", "--no-renames", f"{base}...HEAD"])
    if r.returncode != 0:
        raise Fail((r.stderr or r.stdout).strip() or f"git diff against {base} failed")
    if not r.stdout.strip():
        return ""
    p = subprocess.run(
        ["git", "-C", folder, "patch-id", "--stable"],
        input=r.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    out = p.stdout.strip()
    return out.split()[0] if out else ""


def review_valid(task, commit, patch):
    """True when a recorded review round of `task` matches the final commit.

    A round matches on the same commit hash (`head`) or the same patch-id (`patch_id`, stored by
    `herdr-task reviewed` next to the hash). An empty `commit` or `patch` never matches, so a Task
    with nothing to review is not accepted by accident.
    """
    for rnd in task.get("reviews") or []:
        if commit and rnd.get("head") == commit:
            return True
        if patch and rnd.get("patch_id") == patch:
            return True
    return False
