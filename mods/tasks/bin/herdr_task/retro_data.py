"""The retro JSON a retro agent reads (proposal 6.15, contract C5).

`retro-data <feature>` prints the digests of all a Feature's children plus the state of the repo
check script and CI. The retro agent (templates/retro.md) uses it to suggest improvements; it is
read-only. A Feature with no children is still a valid result (its `digests` is `[]`).
"""

import datetime
import json
import os

from .base import Fail
from .store import open_store, parse_ref
from .output import say
from .github import gh

# Candidate repo check scripts, in order: (the command to run, the file that must exist). The
# first existing one is the repo's check script. `bin/sync-shared --check` is Shepherdr's quick
# check (AGENTS.md, and the contract example).
CHECK_CANDIDATES = [
    ("bin/sync-shared --check", "bin/sync-shared"),
    ("bin/check", "bin/check"),
    ("scripts/check", "scripts/check"),
]

CHECK_TIMEOUT = 300


def build(s, feature):
    """The retro JSON (C5): the Feature, its children's digests, and the check-script and CI state."""
    folder = s.loc["folder"]
    return {
        "feature": {
            "ref": feature.get("ref") or s.ref(feature),
            "title": feature.get("title"),
            "change": feature.get("change"),
        },
        "digests": list((feature.get("digests") or {}).values()),
        "checks": check_state(folder),
        "ci": ci_state(folder),
    }


def _iso(ts=None):
    t = ts or datetime.datetime.now(datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_state(folder):
    """The repo's check script and its current result, or None when the repo has no check script.
    The check is run now (it is a quick gate by repo convention) to fill `last`."""
    import subprocess

    found = next(((cmd, probe) for cmd, probe in CHECK_CANDIDATES
                  if os.path.isfile(os.path.join(folder, probe))), None)
    if not found:
        return None
    command, _ = found
    try:
        r = subprocess.run(command.split(), cwd=folder, capture_output=True, text=True, timeout=CHECK_TIMEOUT)
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return {"script": command, "last": {"ok": ok, "ran_at": _iso()}}


def ci_state(folder):
    """The repo's CI workflow and its last run, or None when the repo has no CI. `last` is None
    when `gh` cannot be reached or the workflow has no runs yet."""
    wf_dir = os.path.join(folder, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return None
    files = sorted(f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml")))
    if not files:
        return None
    name = "check.yml" if "check.yml" in files else files[0]
    workflow = os.path.join(".github", "workflows", name)
    last = None
    ok, out, _ = gh("run", "list", "--workflow", name, "--limit", "1",
                    "--json", "conclusion,url", cwd=folder)
    if ok and out:
        try:
            runs = json.loads(out)
        except ValueError:
            runs = []
        if runs:
            last = {"conclusion": runs[0].get("conclusion"), "url": runs[0].get("url")}
    return {"workflow": workflow, "last": last}


def _find_feature(s, ref):
    """The Feature record for a ref in this folder, or a hard error if it is a Task or missing."""
    pre, n = parse_ref(ref)
    if pre and pre != s.prefix:
        raise Fail(f"{pre}-{n} isn't in {s.loc['folder']} (its refs are {s.prefix}-n).")
    for f in s.data.get("features") or []:
        if f.get("id") == n:
            return f
    if any(t.get("id") == n for t in s.data.get("tasks") or []):
        raise Fail(f"{s.prefix}-{n} is a Task, not a Feature.")
    raise Fail(f"No Feature {s.prefix}-{n} in {s.loc['folder']}.")


def cmd_retro_data(args):
    """The `retro-data` command entry: prints the retro JSON for a Feature (C5).

    `build` runs the repo check script (up to CHECK_TIMEOUT) and shells `gh`, so it runs after
    the store lock is released: the Feature is read under the lock, then `s` (its data and loc
    stay in memory, read-only) is used to build without holding the lock, so a slow check never
    blocks other writers of the store."""
    with open_store(args, write=False) as s:
        feature = _find_feature(s, args.id)
    data = build(s, feature)
    say(args, data, json.dumps(data, indent=2))
