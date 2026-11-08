"""Fan-out: turn an approved work table into the child Tasks of a Feature (proposal section 6.7,
contract C7 task T05b).

`plan` is the dry run (contract C5 shape): it reads `openspec/changes/<change>/tasks.md`, checks the
work table through `worktable` (T05a), groups the rows into work packages, orders them, and places
them across machines with chain affinity (proposal section 6.13). It creates nothing.

`run` does the same and then creates the child Tasks in that order (one package per child), adds the
first Verify Task that waits for them all, and queues them so the existing queue starts them as slots
free up. It is idempotent: a second run adds only the missing children (property of section 6.7), and
`--amend` reads an updated table, adds the new rows and cancels an unstarted child of a removed row.

`check_plan` is the plan guard that `ready` runs on a spec Task (contract C8): `openspec validate`
plus a dry run must both pass before the plan can be approved.

The glossary words (proposal section 2) are the ones used here: Feature, child Task, Verify Task,
work table, row, wave, chain, work package, fan-out, placement, chain affinity, amendment. The module
depends only on `worktable` and `store` (contract C11); it never imports the modules that other T05-
wave tasks build in parallel, and it reaches another machine only through `herdr-task --on <machine>`.
"""

import json
import os
import re
import subprocess
import sys

from .base import ENTRY, Fail, guard, now, run as run_cmd
from .settings import set_task, settings_for
from .store import locate, open_store, Store
from .queues import set_after
from .output import say
from . import worktable


# ---- the dry-run plan (contract C5) ---------------------------------------------------------

def plan(s, change, machines=None, amend=False):
    """The dry-run plan for `change` (contract C5): the waves, the work packages with their owns,
    dependencies, risk, machine, branch, base, after links, merge mode and generated brief, and the
    Verify Task's after list. Creates nothing.

    Raises Fail when the folder has no Feature for the change. Returns the invalid shape
    {"change", "valid": False, "code", "error"} when the work table is bad (the codes of C5/C6)."""
    feature = _feature_for(s, change)
    if machines and feature.get("mode") == "local":
        raise Fail(f"{s.ref(feature)} is in local mode (no GitHub remote): it runs on one machine "
                   f"only, so fan-out cannot place children on {', '.join(machines)}.")
    try:
        rows = _read_rows(s.loc["folder"], change)
        worktable.check_table(rows)
    except worktable.WorkTableError as e:
        return {"change": change, "valid": False, "code": e.code, "error": e.message}

    worktable.add_overlap_links(rows)
    waves = worktable.waves(rows)
    row_order = worktable.order(rows)
    packages = _packages(rows, waves, row_order, s)

    machine_of = _place(packages, machines)
    for pkg in packages:
        pkg["machine"] = machine_of[pkg["id"]]

    refs = _predict_refs(s, feature, packages)

    out = []
    for pkg in packages:
        machine = refs[pkg["id"]]["machine"]
        out.append({
            "package": pkg["id"],
            "rows": pkg["rows"],
            "wave": pkg["wave"],
            "owns": pkg["owns"],
            "depends": pkg["depends"],
            "risk": pkg["risk"],
            "risk_reason": pkg["risk_reason"],
            "size": pkg["size"],
            "machine": machine,
            "branch": _child_branch(s, feature, refs[pkg["id"]]["ref"], pkg),
            "base": feature["branch"],
            "after": _after_refs(pkg, packages, refs, machine),
            "merge": "ask" if pkg["risk"] == "high" else "checks",
            "brief": _child_brief(s, feature, change, pkg),
        })

    return {
        "change": change,
        "feature": s.ref(feature),
        "valid": True,
        "waves": waves,
        "packages": out,
        "verify_task": {"after": [refs[p["id"]]["ref_q"] for p in packages]},
        "warnings": _amend_warnings(s, feature, packages) if amend else [],
    }


def dry_run_json(plan_dict):
    """The plan shape for `--json` (contract C7). The plan is already JSON-ready, so this is an
    identity copy; it exists so callers and the contract have one name, and so a later version can
    reshape the printed form without touching `plan`."""
    return dict(plan_dict)


# ---- the real run ---------------------------------------------------------------------------

def run(s, change, machines=None, amend=False, start=True):
    """Create the missing child Tasks of `change` in plan order, add (or extend) the Verify Task that
    waits for them, and queue them when `start` (the queue then starts them as slots free up). Returns
    the plan (contract C5) with `"created": {"children": n, "verify": 0|1}` and `"started": n`.

    Idempotent: a child already recorded on the Feature (matched by its work-package rows) is left as
    it is, so a second run only fills the gaps. With `amend`, an unstarted child of a row that the
    table no longer has is cancelled, and a started one is kept with a warning (section 6.7)."""
    p = plan(s, change, machines, amend)
    if not p.get("valid"):
        raise Fail(p.get("error") or "the work table is not valid")
    feature = _feature_for(s, change)

    cancelled = _amend_cancel(s, feature, p["packages"]) if amend else []

    existing = _children_by_rows(feature)
    created = 0
    queued_at = now()
    for entry in p["packages"]:
        if frozenset(entry["rows"]) in existing:
            continue
        _create_child(s, feature, entry, start, queued_at)
        created += 1
        queued_at += 1

    _link_children(s, feature, p["packages"])
    verify_made = _ensure_verify(s, feature, change, start, queued_at)
    s.dirty = True

    p["created"] = {"children": created, "verify": 1 if verify_made else 0}
    p["started"] = (created + (1 if verify_made else 0)) if start else 0
    if cancelled:
        p["cancelled"] = cancelled
    return p


def check_plan(folder, change):
    """The `ready` plan guard for a spec Task (contract C7/C8): `openspec validate <change>` and a
    dry run must both pass. Returns {"ok": bool, "detail": str}; `detail` names the first failure."""
    ok, detail = _openspec_valid(folder, change)
    if not ok:
        return {"ok": False, "detail": detail}
    try:
        with Store(locate(folder), write=False) as s:
            p = plan(s, change)
    except Fail as e:
        return {"ok": False, "detail": str(e)}
    if not p.get("valid"):
        return {"ok": False, "detail": p.get("error") or "the work table is not valid"}
    return {"ok": True, "detail": ""}


# ---- the command entry ----------------------------------------------------------------------

def cmd_fanout(args):
    """The `fanout` command (contract C4/C5). `--dry-run` prints the plan and creates nothing; a real
    run creates and (unless `--no-start`) queues the children."""
    change = args.change
    machines = [m.strip() for m in (getattr(args, "machines", None) or "").split(",") if m.strip()] or None
    amend = bool(getattr(args, "amend", False))
    dry = bool(getattr(args, "dry_run", False))
    with open_store(args, write=not dry) as s:
        if dry:
            p = plan(s, change, machines, amend)
            _say_plan(args, p)
            return 0 if p.get("valid") else 1
        p = run(s, change, machines, amend, start=not getattr(args, "no_start", False))
    _say_plan(args, p)
    return 0


def _say_plan(args, p):
    if getattr(args, "json", False):
        say(args, p, "")
        return
    if not p.get("valid"):
        print(f"fanout: the work table is not valid ({p.get('code')}): {p.get('error')}")
        return
    lines = [f"{p['feature']} fan-out of {p['change']}: {len(p['packages'])} work packages"]
    for pkg in p["packages"]:
        where = f" on {pkg['machine']}" if pkg["machine"] else ""
        risk = f" [high: {pkg['risk_reason']}]" if pkg["risk"] == "high" else ""
        after = f" after {', '.join(pkg['after'])}" if pkg["after"] else ""
        lines.append(f"  {pkg['package']} ({', '.join(pkg['rows'])}) {pkg['branch']}{where}{risk}{after}")
    lines.append(f"  verify after {', '.join(p['verify_task']['after']) or '(nothing)'}")
    for w in p.get("warnings") or []:
        lines.append(f"  warning: {w}")
    if "created" in p:
        lines.append(f"created {p['created']['children']} child Tasks"
                     + (" and 1 Verify Task" if p["created"]["verify"] else "")
                     + (f", queued {p['started']}" if p["started"] else ""))
    print("\n".join(lines))


# ---- the Feature and the work table ---------------------------------------------------------

def _feature_for(s, change):
    """The Feature record of `change` in this folder, or a Fail. Fan-out is always for a Feature; the
    feature branch, base and ref all come from here."""
    for f in s.data.get("features", []):
        if f.get("change") == change:
            return f
    raise Fail(f"No feature for change {change!r} in {s.loc['folder']}. Create it with `feature add` first.")


def _read_rows(folder, change):
    """The rows of `openspec/changes/<change>/tasks.md`; a WorkTableError(BAD_TABLE) when the file is
    missing (so a caller treats it like any other bad table)."""
    path = os.path.join(folder, "openspec", "changes", change, "tasks.md")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        raise worktable.WorkTableError(worktable.BAD_TABLE, f"no tasks.md at openspec/changes/{change}/")
    return worktable.parse_table(text)


# ---- work packages (section 6.7 step 5) -----------------------------------------------------

def _packages(rows, waves, row_order, s):
    """Group the rows into work packages (section 6.7 step 5). A package holds rows of one wave and one
    directory; one row is one package by default (the shape the contract fixtures and C5 example show,
    and the one that gives the most parallel children, which is the point of the flow). Returns the
    packages in run order (`row_order`), each with its id (the row id), its rows, wave, owns, external
    row dependencies, risk, risk reason and size."""
    wave_of = {rid: depth for depth, ids in enumerate(waves) for rid in ids}
    order_pos = {rid: i for i, rid in enumerate(row_order)}
    risky = settings_for(s)["tasks"].get("risky_paths") or []

    packages = []
    for r in sorted(rows, key=lambda r: order_pos[r["id"]]):
        risk, reason = _risk_of(r, risky)
        packages.append({
            "id": r["id"],
            "rows": [r["id"]],
            "wave": wave_of[r["id"]],
            "owns": list(r["owns"]),
            "depends": list(r["depends"]),
            "risk": risk,
            "risk_reason": reason,
            "size": r["size"],
            "title": r["title"],
        })
    return packages


def _risk_of(row, risky):
    """A row's risk and its reason (contract C5): high when the risk column is high, or when an owned
    path matches a risky-path glob (proposal section 6.6)."""
    if row["risk"] == "high":
        return "high", "risk column is high"
    for path in row["owns"]:
        g = _risky_match(path, risky)
        if g:
            return "high", f"owns matches risky path {g}"
    return "low", ""


def _risky_match(path, risky):
    """The first risky-path glob that `path` matches (segment-aware, like the owned-paths check), or
    None."""
    for g in risky:
        if worktable._patterns_overlap(path, g):
            return g
    return None


def _dedup(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


# ---- placement with chain affinity (section 6.13) -------------------------------------------

def _place(packages, machines):
    """Place each package on a machine. With no `machines`, every package stays on the home machine
    (machine None). With a machine list, a chain of dependent packages stays on one machine (chain
    affinity), and independent chains spread across the machines round-robin. The result is
    deterministic: it depends only on the packages and the machine list."""
    if not machines:
        return {pkg["id"]: None for pkg in packages}

    ids = [pkg["id"] for pkg in packages]
    order_pos = {pid: i for i, pid in enumerate(ids)}
    deps = {pkg["id"]: _pkg_depends(pkg, packages) for pkg in packages}
    successors = {pid: [] for pid in ids}
    for pid, ps in deps.items():
        for q in ps:
            successors[q].append(pid)
    chain = _pkg_chain_len(ids, successors)

    def primary_succ(q):
        kids = successors[q]
        return max(kids, key=lambda k: (chain[k], -order_pos[k])) if kids else None

    machine_of = {}
    rr = 0
    for pid in ids:
        parents = [q for q in deps[pid] if primary_succ(q) == pid]
        if parents:
            parent = max(parents, key=lambda q: (chain[q], -order_pos[q]))
            machine_of[pid] = machine_of[parent]
        else:
            machine_of[pid] = machines[rr % len(machines)]
            rr += 1
    return machine_of


def _pkg_depends(pkg, packages):
    """The package ids this package depends on: the package that holds each external row dependency,
    in order, without duplicates."""
    holder = {rid: other["id"] for other in packages for rid in other["rows"]}
    out = []
    for d in pkg["depends"]:
        pid = holder.get(d)
        if pid and pid != pkg["id"] and pid not in out:
            out.append(pid)
    return out


def _pkg_chain_len(ids, successors):
    """Each package id to the length of the longest chain of packages that depend on it (downstream)."""
    chain = {}

    def compute(pid):
        if pid not in chain:
            kids = successors[pid]
            chain[pid] = 0 if not kids else 1 + max(compute(k) for k in kids)
        return chain[pid]

    for pid in ids:
        compute(pid)
    return chain


# ---- refs, branches and after links ---------------------------------------------------------

def _predict_refs(s, feature, packages):
    """A map from package id to {ref, ref_q, machine} for the plan. An existing child keeps its real
    ref and machine; a missing one is predicted from the folder counter in package order, with this
    folder's prefix (the prediction a real run realises for home children). `ref_q` is the ref a task
    on another machine is named by (machine:ref), the plain ref on the home machine."""
    existing = _children_by_rows(feature)
    nxt = s.data.get("next", 1)
    refs = {}
    for pkg in packages:
        c = existing.get(frozenset(pkg["rows"]))
        if c:
            ref, machine = f"{s.prefix}-{c['ref']}", c.get("machine")
        else:
            ref, machine = f"{s.prefix}-{nxt}", pkg.get("machine")
            nxt += 1
        refs[pkg["id"]] = {"ref": ref, "machine": machine, "ref_q": _qualify(ref, machine)}
    return refs


def _after_refs(pkg, packages, refs, machine):
    """The refs this package comes after: the child of each package it depends on. A dependency on
    another machine is named machine:ref (proposal section 6.13, the cross-wave links). A root package
    (no package dependency) comes after nothing: the fan-out trigger only runs once the spec Task has
    merged onto the feature branch (contract C8), so the children branch from a feature branch that
    already holds the plan."""
    out = []
    for pid in _pkg_depends(pkg, packages):
        dep = refs[pid]
        out.append(dep["ref_q"] if dep["machine"] != machine else dep["ref"])
    return out


def _qualify(ref, machine):
    return f"{machine}:{ref}" if machine else ref


def _child_branch(s, feature, child_ref, pkg):
    """A child Task's branch: task/<FEATURE-REF>-<CHILD-REF>-<short-title> (proposal section 6.4)."""
    child = child_ref.split(":")[-1]
    return f"task/{s.ref(feature)}-{child}-{_slug(pkg)}"


def _slug(pkg):
    """A short branch slug from the package's first row title, else its id."""
    words = re.sub(r"[^a-z0-9]+", "-", (pkg.get("title") or "").lower()).strip("-")[:32].strip("-")
    return words or pkg["id"].replace(".", "-")


# ---- the generated child brief --------------------------------------------------------------

def _child_brief(s, feature, change, pkg):
    """The per-child brief (the plan's `brief`, the Task's prompt). It names the work package and its
    rules (proposal section 6.8); the full flow-task template is added later by `brief()` (T15)."""
    lines = [
        f"Feature {s.ref(feature)}: {feature['title']}.",
        f"Your work package is {pkg['id']} of the change {change}: rows {', '.join(pkg['rows'])}.",
        "",
        "Owned paths (change only these):",
    ]
    lines += [f"  {o}" for o in pkg["owns"]]
    lines += [
        "",
        "Do only this work package. Run /opsx:apply for your rows only; do not edit tasks.md and do",
        "not tick its boxes. Run `herdr-task sync-branch` before `ready` and when the feature branch",
        "moves. If you need a file that is not yours, set your status to \"needs owns change\" and ask",
        "the Assistant to extend your owned paths. Never push to the base branch.",
    ]
    return "\n".join(lines)


# ---- creating the Tasks ---------------------------------------------------------------------

def _create_child(s, feature, entry, start, queued_at):
    """Create one child Task for a work package (contract C2 fields), on this machine or, when the
    package is placed elsewhere, on that machine with `herdr-task --on`. Records it on the Feature's
    `children` list."""
    if entry["machine"]:
        _create_remote_child(s, feature, entry, start)
        return
    t = s.new(
        _child_title(entry),
        entry["brief"],
        mode="worktree",
        feature=feature["id"],
        role="child",
        owns=list(entry["owns"]),
        risk=entry["risk"],
        rows=list(entry["rows"]),
        retries=0,
        skipped=False,
    )
    t["branch"] = entry["branch"]
    t["base"], t["base_set"] = feature["branch"], True
    set_task(t, "merge", entry["merge"])
    if start:
        t["state"], t["queued_at"] = "queued", queued_at
    feature.setdefault("children", []).append(
        {"ref": t["id"], "machine": None, "package": entry["package"], "rows": list(entry["rows"]), "role": "child"})
    s.dirty = True


def _child_title(entry):
    return f"{entry['package']}: {', '.join(entry['rows'])}"


def _create_remote_child(s, feature, entry, start):
    """Create a child on another machine with `herdr-task --on <machine> add` (proposal section 6.13).
    Only the home machine runs fan-out; this is how it places a package elsewhere. Not reached by the
    tests, which never run a changed herdr-task on other machines (a repo rule)."""
    machine = entry["machine"]
    argv = ["--on", machine, "add", _child_title(entry), "--worktree", "--prompt", entry["brief"],
            "--branch", entry["branch"], "--base", feature["branch"], "--folder", s.loc["folder"],
            "--feature", s.ref(feature), "--role", "child", "--risk", entry["risk"],
            "--owns", ",".join(entry["owns"]), "--set", f"merge={entry['merge']}", "--json"]
    if entry["after"]:
        argv += ["--after", ",".join(entry["after"])]
    guard(["hc"])
    r = subprocess.run([sys.executable, ENTRY, *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = None
    if r.returncode != 0 or not isinstance(d, dict) or "id" not in d:
        raise Fail(f"Couldn't create {entry['package']} on {machine}: "
                   f"{(r.stderr or '').strip() or 'no task returned'}")
    feature.setdefault("children", []).append(
        {"ref": d["id"], "machine": machine, "package": entry["package"], "rows": list(entry["rows"]), "role": "child"})
    s.dirty = True


def _link_children(s, feature, packages_plan):
    """Set each home child's `after` to the children of the packages it depends on (contract: the
    queue then starts a child only once its dependencies are done). A dependency on another machine is
    added as an external `--after` (proposal section 6.13, the cross-wave links)."""
    by_pkg = {c["package"]: c for c in feature.get("children", [])}
    holder = {rid: entry["package"] for entry in packages_plan for rid in entry["rows"]}
    for entry in packages_plan:
        c = by_pkg.get(entry["package"])
        if not c or c.get("machine"):
            continue
        t = s.task_by_id(c["ref"])
        if not t:
            continue
        home_ids, ext = [], []
        for dep_pkg in _dedup(holder[d] for d in entry["depends"] if holder.get(d) and holder[d] != entry["package"]):
            dc = by_pkg.get(dep_pkg)
            if not dc:
                continue
            if dc.get("machine"):
                ext.append({"ref": _ref_of(s, dc), "machine": dc["machine"], "prefix": s.prefix, "id": dc["ref"]})
            else:
                home_ids.append(dc["ref"])
        if (home_ids or ext) and ((t.get("after") or []) != home_ids or ext):
            set_after(s, t, home_ids, ext)


def _ensure_verify(s, feature, change, start, queued_at):
    """Create the first Verify Task (role verify) that waits for every child, or extend the existing
    one's `after` with any newly added children. Returns True when a Verify Task was created."""
    home_ids = [c["ref"] for c in feature.get("children", []) if not c.get("machine")]
    if feature.get("verify_task"):
        t = s.task_by_id(feature["verify_task"])
        if t and home_ids and (t.get("after") or []) != home_ids:
            set_after(s, t, home_ids)
        return False
    t = s.new(
        f"Verify {s.ref(feature)}: {feature['title']}",
        f"Run /opsx:verify {change} on the feature branch {feature['branch']}. You are read-only: do "
        f"not change code. Report with `herdr-task verify-report --pass` or `--findings-file <file>`.",
        mode="worktree",
        feature=feature["id"],
        role="verify",
        owns=[],
    )
    t["branch"] = f"task/{s.ref(feature)}-{s.ref(t)}-verify"
    t["base"], t["base_set"] = feature["branch"], True
    if home_ids:
        set_after(s, t, home_ids)
    if start:
        t["state"], t["queued_at"] = "queued", queued_at
    feature["verify_task"] = t["id"]
    s.dirty = True
    return True


# ---- amendment (section 6.7) ----------------------------------------------------------------

def _amend_warnings(s, feature, packages):
    """The warnings an amendment prints (section 6.7): a started child of a removed row. The running
    child's owns are never changed under it, so the plan keeps its old package; a changed owns shows
    here too when the row stays but its paths move."""
    warnings = []
    all_rows = {r for pkg in packages for r in pkg["rows"]}
    for c in feature.get("children", []):
        rows = set(c.get("rows") or [])
        if rows and not (rows & all_rows):
            t = _child_record(s, c)
            if t and _started(t):
                warnings.append(
                    f"row(s) {', '.join(sorted(rows))} removed, but child {_ref_of(s, c)} has started; it stays")
    return warnings


def _amend_cancel(s, feature, packages):
    """Cancel an unstarted child of a row the table no longer has (section 6.7). A started child is
    kept (its warning comes from the plan). Returns the refs of the cancelled children."""
    all_rows = {r for pkg in packages for r in pkg["rows"]}
    cancelled = []
    for c in list(feature.get("children", [])):
        rows = set(c.get("rows") or [])
        if rows and not (rows & all_rows) and not c.get("machine"):
            t = s.task_by_id(c["ref"])
            if t and not _started(t):
                t["state"] = "cancelled"
                for k in ("queued_at", "queue", "held", "scheduled_for"):
                    t.pop(k, None)
                cancelled.append(_ref_of(s, c))
    if cancelled:
        feature["children"] = [c for c in feature["children"] if _ref_of(s, c) not in cancelled]
        s.dirty = True
    return cancelled


def _started(task):
    """True once a Task has left the queue: it is running, ready, done or past that (not still todo,
    queued, scheduled or already cancelled)."""
    return task.get("state") not in ("todo", "queued", "scheduled", "cancelled")


# ---- small helpers --------------------------------------------------------------------------

def _children_by_rows(feature):
    """A map from a child's work-package row set (frozenset) to its Feature `children` entry, so a
    rerun matches a package to the child that already does it (idempotent fan-out)."""
    return {frozenset(c.get("rows") or []): c for c in feature.get("children", [])}


def _child_record(s, child):
    return None if child.get("machine") else s.task_by_id(child["ref"])


def _ref_of(s, child):
    return _qualify(f"{s.prefix}-{child['ref']}", child.get("machine"))


def _openspec_valid(folder, change):
    """Run `openspec validate <change> --json` in `folder`. Returns (ok, detail). openspec exits 0
    even for an invalid change, so the verdict comes from the parsed JSON, not the exit code."""
    try:
        r = run_cmd(["openspec", "validate", change, "--json", "--no-interactive"], cwd=folder)
    except Exception as e:  # openspec not installed, or not runnable
        return False, f"could not run openspec validate: {e}"
    try:
        d = json.loads(r.stdout)
    except ValueError:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else "openspec validate gave no output"
    items = d.get("items") or []
    if not items:
        return False, f"openspec did not validate the change {change!r}"
    bad = [it for it in items if not it.get("valid")]
    if bad:
        issues = [i.get("message", "") for it in bad for i in (it.get("issues") or [])]
        return False, "; ".join(x for x in issues if x) or "the change did not validate"
    return True, ""
