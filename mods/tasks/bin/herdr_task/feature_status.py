"""The states and digests of a Feature's children on one machine, in one call (proposal 6.13,
contract C7 `feature_status`).

A Feature lives on the home machine, but its children run on any machine (proposal 6.13). To
move a child's state and digest to the home machine, the home machine asks each machine once in
each tick:

- `collect(s, feature)` is the local answer: the states and digests of the children on this
  machine (the `feature-status` shape of C5). "This machine" is the home machine when the Feature
  record is in this store (home children carry `machine` null, C2), else the herdr-core name of
  this machine (a remote's children carry its name).
- `gather(s, feature, machines)` runs on the home machine: it collects the local children once,
  then makes one `herdr-task feature-status --on <machine>` call per other machine, and merges
  the results. A machine that cannot be reached is marked `reachable: false` with no children, and
  the home machine goes on with the others (proposal R8).
- `cmd_feature_status(args)` is the `feature-status` command entry: with `--on <machine>` it asks
  that one machine, otherwise it answers for this machine.

A child's digest is built on the machine where its session log lives (`digest.build`), so a child
that has ended carries a real digest in the answer; a child still running carries `null`.
"""

import json
import subprocess
import sys

from . import digest
from .base import ENTRY, hc_json
from .output import say
from .store import open_store

# How long to wait for one machine to answer before it counts as unreachable (seconds). A machine
# that is offline or slow does not hold up the rest of the tick.
REMOTE_TIMEOUT = 60

# A child Task state that means the child has ended, so its digest can be built (C5). A child in
# any other state (todo, queued, running, ready) carries `digest: null`.
ENDED_STATES = {"done", "cancelled", "failed"}


# ---- one machine's children (local) ---------------------------------------------------------

def collect(s, feature):
    """The `feature-status` shape (C5) for the children of `feature` that run on this machine.

    The Feature record passed in carries the whole `children` list (C2); this machine answers only
    for the children whose `machine` is this one. The home machine (it holds the Feature record)
    answers for the children with `machine` null; a remote machine answers for the children that
    carry its name."""
    ref = _feature_ref(s, feature)
    me = _local_machine(s, feature)
    children = [
        _child_status(s, feature, child)
        for child in (feature.get("children") or [])
        if (child.get("machine") or None) == me
    ]
    return {"feature": ref, "machine": me, "reachable": True, "children": children}


def _child_status(s, feature, child):
    """One child's `{ref, state, role, rows, digest}` (C5). The digest is the one already stored on
    the Feature when present (C2), else it is built now for a child that has ended (so a remote, a
    child's own machine, builds the digest the home machine does not have yet); a child still
    running carries `null`."""
    cid = child.get("ref")
    task = s.task_by_id(cid) if isinstance(cid, int) else None
    status = {
        "ref": _child_ref(s, feature, cid),
        "state": task.get("state") if task else None,
        "role": child.get("role"),
        "rows": list(child.get("rows") or []),
        "digest": None,
    }
    if task is not None and _ended(task):
        stored = (feature.get("digests") or {}).get(digest.child_key(child))
        status["digest"] = stored or digest.build(s, task)
    return status


def _ended(task):
    """True when a child Task has ended, so its digest can be built (C5): a skipped or cancelled
    row, or a Task in an ended state."""
    return bool(task.get("skipped")) or bool(task.get("cancelled")) or task.get("state") in ENDED_STATES


def _feature_ref(s, feature):
    """The Feature's ref (`HAT-12`): the one the record already carries (the service enriches it,
    C9), else computed from this folder's prefix."""
    return feature.get("ref") or s.ref(feature)


def _child_ref(s, feature, cid):
    """A child's ref as the Feature names it: the Feature's own prefix with the child's id, so a
    child reads the same ref on every machine (the home prefix), not the local machine's prefix."""
    fref = _feature_ref(s, feature)
    prefix = fref.rsplit("-", 1)[0] if "-" in fref else s.prefix
    return f"{prefix}-{cid}"


def _local_machine(s, feature):
    """None when this is the home machine (the Feature record lives in this store, so its children
    with `machine` null are this machine's), else this machine's herdr-core name (a remote answers
    for the children that carry its name)."""
    fid = feature.get("id")
    if fid is not None and s.feature_by_id(fid) is not None:
        return None
    return _this_machine()


def _this_machine():
    """This machine's herdr-core name, or None. (The same read as `digest._this_machine`; kept
    here so this module does not lean on another module's private helper.)"""
    try:
        d = hc_json("server", "status") or {}
        return (d.get("core") or {}).get("name") or None
    except Exception:
        return None


# ---- every machine, from the home machine ---------------------------------------------------

def gather(s, feature, machines):
    """The children of `feature` across every machine, gathered on the home machine (C7).

    The local children are collected once (no call out), then one `feature-status --on <machine>`
    call is made for each other machine in `machines`. The results are merged into one object; a
    machine that cannot be reached is `reachable: false` with no children, and does not stop the
    others (proposal R8). `machines` may hold the local machine (null or `"local"`): it is skipped,
    since the local children are already collected, so there is exactly one call per other machine."""
    ref = _feature_ref(s, feature)
    results = [collect(s, feature)]
    for machine in _remote_machines(machines):
        results.append(_ask_machine(ref, machine))
    children = [c for r in results for c in (r.get("children") or [])]
    unreachable = [r.get("machine") for r in results if not r.get("reachable")]
    return {"feature": ref, "machines": results, "children": children, "unreachable": unreachable}


def _remote_machines(machines):
    """The distinct other machines to ask, in order: the local machine (null, "" or "local") is
    dropped, and a repeated name is asked only once (one call per machine)."""
    seen, out = set(), []
    for m in machines or []:
        if m in (None, "", "local") or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def _ask_machine(ref, machine, timeout=REMOTE_TIMEOUT):
    """One `herdr-task feature-status <ref> --on <machine>` call, as the home machine asking that
    machine. Returns the machine's `feature-status` object, or the unreachable shape when the
    machine does not answer, answers with an error, or answers with something unreadable (so one
    bad machine never stops the gather)."""
    args = [sys.executable, ENTRY, "--on", machine, "feature-status", str(ref), "--json"]
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return _unreachable(ref, machine)
    if r.returncode != 0:
        return _unreachable(ref, machine)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return _unreachable(ref, machine)
    if not isinstance(d, dict) or "error" in d or not isinstance(d.get("children"), list):
        return _unreachable(ref, machine)
    d["feature"] = ref
    d["machine"] = machine
    d["reachable"] = bool(d.get("reachable", True))
    return d


def _unreachable(ref, machine):
    """The answer for a machine that could not be reached (C5): no children, `reachable: false`."""
    return {"feature": ref, "machine": machine, "reachable": False, "children": []}


# ---- the command ----------------------------------------------------------------------------

def cmd_feature_status(args):
    """The `feature-status` command entry (C4). With `--on <machine>` it asks that machine and
    prints its answer; otherwise it answers for this machine from its own store."""
    ref = args.id
    machine = getattr(args, "on", None)
    if machine:
        data = _ask_machine(ref, machine)
    else:
        # Build the digests (a child's log is replayed) after the store lock is released, as
        # `retro_data` does, so a slow replay never blocks other writers of the store.
        with open_store(args, write=False) as s:
            feature = s.find_feature(ref)
        data = collect(s, feature)
    say(args, data, _text(data))


def _text(data):
    """One block of plain text for a terminal (no `--json`): the machine, then one line per child."""
    head = f"{data.get('feature')} on {data.get('machine') or 'this machine'}"
    if not data.get("reachable", True):
        head += " (unreachable)"
    lines = [head]
    for c in data.get("children") or []:
        mark = "digest" if c.get("digest") else "-"
        rows = ", ".join(c.get("rows") or [])
        lines.append(f"  {c.get('ref') or '?':<10} {c.get('state') or '?':<9} {rows:<10} {mark}")
    return "\n".join(lines)
