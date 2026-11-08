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
plus a dry run must both pass before the plan can be approved. The spec worker runs `ready` in its own
worktree, so `check_plan` reads the work table and validates from that worktree, not from the main
checkout the Feature record lives in.

The after links and the Verify Task are built from the Feature's own `children` list (the records that
were actually created), never from the plan's predicted refs, so a real run is correct even when the
counter has moved or a child already sits on another machine.

Placement across machines (section 6.13) is computed and shown in the plan, so `fanout --dry-run
--machines a,b,c` previews the spread with chain affinity. A real run, though, creates children on the
home machine only: creating a C2-complete child on another machine needs the `add` command to carry
the child's `rows` (contract C4 has no such flag) and a lock-safe way to make cross-machine calls
without holding the folder's store lock; both are missing, so a real `--machines` run is refused with a
clear message rather than shipping a child that is wrong or a run that deadlocks. The Verify Task and
the after links still treat a child on any machine correctly (so the multi-machine integration can
record remote children later and this module links them), but this module does not itself create them.

The glossary words (proposal section 2) are the ones used here: Feature, child Task, Verify Task, work
table, row, wave, chain, work package, fan-out, placement, chain affinity, amendment. The module
depends only on `worktable` and `store` (contract C11); it never imports the modules that other T05-
wave tasks build in parallel.
"""

import json
import os
import re
import shlex

from .base import Fail, git, now, run as run_cmd
from .settings import set_task, settings_for
from .store import locate, open_store, Store
from .queues import set_after
from .output import say
from . import worktable


# ---- the dry-run plan (contract C5) ---------------------------------------------------------

def plan(s, change, machines=None, amend=False, folder=None):
    """The dry-run plan for `change` (contract C5): the waves, the work packages with their owns,
    dependencies, risk, machine, branch, base, after links, merge mode and generated brief, and the
    Verify Task's after list. Creates nothing.

    `folder` overrides where `tasks.md` is read from (the spec worktree for the `ready` plan guard);
    it defaults to the Feature's folder. Raises Fail when the folder has no Feature for the change, or
    when `machines` names other machines for a local-mode Feature. Returns the invalid shape
    {"change", "valid": False, "code", "error"} when the work table is bad (the codes of C5/C6)."""
    feature = _feature_for(s, change)
    if machines and feature.get("mode") == "local":
        raise Fail(f"{s.ref(feature)} is in local mode (no GitHub remote): it runs on one machine "
                   f"only, so fan-out cannot place children on {', '.join(machines)}.")
    try:
        # The approved work table lives on the feature branch (contract C8), which is not the branch
        # checked out where fan-out runs; read it from that branch. `folder` overrides this with a
        # working tree (the spec worktree for the `ready` plan guard, before the merge).
        if folder:
            rows = _read_rows(folder, change)
        else:
            rows = _read_rows(s.loc["folder"], change, branch=feature["branch"])
        worktable.check_table(rows)
    except worktable.WorkTableError as e:
        return {"change": change, "valid": False, "code": e.code, "error": e.message}

    worktable.add_overlap_links(rows)
    waves = worktable.waves(rows)
    row_order = worktable.order(rows)
    packages = _packages(rows, waves, row_order, s, _spec_space(s, feature))

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
            "after": _plan_after(pkg, packages, refs, machine),
            "merge": "ask" if pkg["risk"] == "high" else "checks",
            "brief": _child_brief(s, feature, change, pkg),
        })

    verify_after = [refs[p["id"]]["ref_q"] for p in packages]
    if amend:
        # A started or remote child of a removed row is kept, and the Verify Task still waits for it;
        # show that in the preview so the plan matches what Verify will wait for.
        verify_after += _retained_refs(s, feature, packages)

    return {
        "change": change,
        "feature": s.ref(feature),
        "valid": True,
        "waves": waves,
        "packages": out,
        "verify_task": {"after": verify_after},
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
    waits for them all, and queue them when `start` (the queue then starts them as slots free up).
    Returns the plan (contract C5) with `"created": {"children": n, "verify": 0|1}` and `"started": n`,
    and `"cancelled"` when an amendment cancelled children.

    Idempotent: a child already recorded on the Feature (matched by its work-package rows) is left as
    it is, so a second run only fills the gaps. With `amend`, an unstarted child of a row the table no
    longer has is cancelled, a started one is kept with a warning, and a running child's owns are never
    changed (section 6.7)."""
    p = plan(s, change, machines, amend)
    if not p.get("valid"):
        raise Fail(p.get("error") or "the work table is not valid")
    feature = _feature_for(s, change)

    cancelled = _amend_cancel(s, feature, p["packages"]) if amend else []
    if amend:
        _amend_reconcile_risk(s, feature, p["packages"])

    existing = _children_by_rows(feature)
    # Only a package that still has to be created can be refused for its placement; a child already on
    # another machine (its machine preserved in the plan) is left alone, not re-refused.
    _refuse_remote_creation([pkg for pkg in p["packages"] if frozenset(pkg["rows"]) not in existing])
    space = _spec_space(s, feature)
    created = 0
    queued_at = now()
    for entry in p["packages"]:
        if frozenset(entry["rows"]) in existing:
            continue
        _create_child(s, feature, entry, space, start, queued_at)
        existing = _children_by_rows(feature)  # so a later package sees this one as a dependency
        created += 1
        queued_at += 1

    _link_home_children(s, feature, p["packages"])
    verify_made = _ensure_verify(s, feature, change, space, start, queued_at)
    s.dirty = True

    p["created"] = {"children": created, "verify": 1 if verify_made else 0}
    p["started"] = (created + (1 if verify_made else 0)) if start else 0
    if cancelled:
        p["cancelled"] = cancelled
    return p


def check_plan(folder, change):
    """The `ready` plan guard for a spec Task (contract C7/C8): `openspec validate <change>` and a dry
    run must both pass. The spec worker runs in its own worktree, so both read from `folder` (the
    worktree), while the Feature record is read from the folder's store (the main checkout). Returns
    {"ok": bool, "detail": str}; `detail` names the first failure."""
    ok, detail = _openspec_valid(folder, change)
    if not ok:
        return {"ok": False, "detail": detail}
    try:
        with Store(locate(folder), write=False) as s:
            p = plan(s, change, folder=folder)
    except Fail as e:
        return {"ok": False, "detail": str(e)}
    if not p.get("valid"):
        return {"ok": False, "detail": p.get("error") or "the work table is not valid"}
    return {"ok": True, "detail": ""}


# ---- the command entry ----------------------------------------------------------------------

def cmd_fanout(args):
    """The `fanout` command (contract C4/C5/C6). `--dry-run` prints the plan and creates nothing; a
    real run creates and (unless `--no-start`) queues the children. An invalid work table prints the
    C5 invalid shape and exits 1, for a real run as well as a dry run."""
    change = args.change
    machines = [m.strip() for m in (getattr(args, "machines", None) or "").split(",") if m.strip()] or None
    amend = bool(getattr(args, "amend", False))
    dry = bool(getattr(args, "dry_run", False))
    with open_store(args, write=not dry) as s:
        p = plan(s, change, machines, amend)
        if not p.get("valid"):
            _say_plan(args, p)
            return 1
        if not dry:
            p = run(s, change, machines, amend, start=not getattr(args, "no_start", False))
    if not dry and p.get("started"):
        # The queue starts the children; wake the scheduler as cmd_start does, now the lock is
        # released. (When tick.py drives fan-out, the scheduler is already the caller.)
        try:
            from .scheduler import ensure_scheduler
            ensure_scheduler(quiet=True)
        except Exception:
            pass
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
        lines.append(f"  {pkg['package']} ({', '.join(pkg['rows'])}){where}{risk}")
        lines.append("    herdr-task add " + " ".join(_add_flags(p["feature"], pkg)))
    lines.append(f"  verify after {', '.join(p['verify_task']['after']) or '(nothing)'}")
    for w in p.get("warnings") or []:
        lines.append(f"  warning: {w}")
    if "created" in p:
        lines.append(f"created {p['created']['children']} child Tasks"
                     + (" and 1 Verify Task" if p["created"]["verify"] else "")
                     + (f", queued {p['started']}" if p["started"] else ""))
    for ref in p.get("cancelled") or []:
        lines.append(f"cancelled {ref} (its row was removed)")
    print("\n".join(lines))


def _add_flags(feature_ref, pkg):
    """The exact `add` flags a package becomes (proposal section 6.7 step 10), for the printed plan.
    Values are shell-quoted so a glob such as `src/*.ts` is safe to paste."""
    flags = [shlex.quote(_child_title({"package": pkg["package"], "rows": pkg["rows"]})), "--worktree",
             "--feature", feature_ref, "--role", "child",
             "--owns", shlex.quote(",".join(pkg["owns"])), "--risk", pkg["risk"],
             "--base", shlex.quote(pkg["base"]), "--branch", shlex.quote(pkg["branch"]),
             "--set", f"merge={pkg['merge']}"]
    if pkg["after"]:
        flags += ["--after", shlex.quote(",".join(pkg["after"]))]
    return flags


# ---- the Feature and the work table ---------------------------------------------------------

def _feature_for(s, change):
    """The Feature record of `change` in this folder, or a Fail. Fan-out is always for a Feature; the
    feature branch, base and ref all come from here."""
    for f in s.data.get("features", []):
        if f.get("change") == change:
            return f
    raise Fail(f"No feature for change {change!r} in {s.loc['folder']}. Create it with `feature add` first.")


def _read_rows(folder, change, branch=None):
    """The rows of `openspec/changes/<change>/tasks.md`. With `branch`, read the file from that git
    branch (`git show <branch>:<path>`), without checking it out, so fan-out reads the approved table
    from the feature branch while the main checkout stays on its own branch (contract C8). Without
    `branch`, read the working tree under `folder` (the spec worktree, for the `ready` plan guard).
    Raises WorkTableError(BAD_TABLE) when the file is missing, so a caller treats it like any other bad
    table."""
    rel = f"openspec/changes/{change}/tasks.md"
    if branch:
        ok, out, _ = git(folder, "show", f"{branch}:{rel}")
        if not ok:
            raise worktable.WorkTableError(worktable.BAD_TABLE, f"no {rel} on {branch}")
        text = out
    else:
        try:
            with open(os.path.join(folder, rel)) as f:
                text = f.read()
        except OSError:
            raise worktable.WorkTableError(worktable.BAD_TABLE, f"no tasks.md at {rel}")
    return worktable.parse_table(text)


# ---- work packages (section 6.7 step 5) -----------------------------------------------------

def _packages(rows, waves, row_order, s, space):
    """Group the rows into work packages (section 6.7 step 5). A package holds rows of one wave and one
    directory; one row is one package by default (the shape the contract fixtures and the C5 example
    show, and the one that gives the most parallel children, which is the point of the flow). Returns
    the packages in run order (`row_order`), each with its id (the row id), its rows, wave, owns,
    external row dependencies, risk, risk reason and size.

    `space` is the Feature's space (the children's), so the risky-path setting is read for that space,
    not the first one that shares the folder; a risky path set in the real space is not missed."""
    wave_of = {rid: depth for depth, ids in enumerate(waves) for rid in ids}
    order_pos = {rid: i for i, rid in enumerate(row_order)}
    risky = settings_for(s, space)["tasks"].get("risky_paths") or []

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


# ---- refs and branches (plan display only) --------------------------------------------------

def _predict_refs(s, feature, packages):
    """For the plan display, a map from package id to {ref, ref_q, machine}. An existing child keeps
    its real ref and machine; a missing one is predicted from the folder counter in package order,
    with this folder's prefix. These predictions are for the dry-run preview only; a real run links by
    the children it actually creates (`_link_home_children`, `_ensure_verify`)."""
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


def _plan_after(pkg, packages, refs, machine):
    """The after refs shown in the plan: the child of each package it depends on, machine-qualified
    when it sits on another machine (proposal section 6.13). A root package comes after nothing: the
    fan-out trigger only runs once the spec Task has merged onto the feature branch (contract C8), so
    the children branch from a feature branch that already holds the plan."""
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

def _refuse_remote_creation(packages):
    """A real run creates children on the home machine only. Refuse, before anything is created, to
    place children on other machines: see the module docstring (no `rows` flag on `add`, contract C4;
    and a lock-safe cross-machine call is needed). `fanout --dry-run --machines ...` still previews the
    placement."""
    machines = sorted({pkg["machine"] for pkg in packages if pkg["machine"]})
    if machines:
        raise Fail(
            f"fanout creates children on the home machine only; it cannot place them on "
            f"{', '.join(machines)} yet. Creating a child on another machine needs the `add` command "
            f"to carry its work-package rows (the contract's `add` has no --rows) and a lock-safe "
            f"cross-machine call; both are missing. Preview the spread with `fanout --dry-run "
            f"--machines {','.join(machines)}`, or run without --machines.")


def _spec_space(s, feature):
    """The space the Feature's children run in: the spec Task's space (set when it ran). A child with no
    space makes the queue fall back to the default space, which can be the wrong one, so carry it."""
    spec = s.task_by_id(feature.get("spec_task")) if feature.get("spec_task") else None
    return spec.get("space") if spec else None


def _create_child(s, feature, entry, space, start, queued_at):
    """Create one child Task for a work package (contract C2 fields) on this machine, and record it on
    the Feature's `children` list. The child's after links are set later by `_link_home_children`, once
    every package it depends on exists. A real run never reaches this with a remote placement
    (`_refuse_remote_creation` stops that first)."""
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
    if space:
        t["queue"] = {"space": space}  # the space the queue starts it in (the spec Task's)
    if start:
        t["state"], t["queued_at"] = "queued", queued_at
    feature.setdefault("children", []).append(
        {"ref": t["id"], "machine": None, "package": entry["package"], "rows": list(entry["rows"]), "role": "child"})
    s.dirty = True


def _child_title(entry):
    return f"{entry['package']}: {', '.join(entry['rows'])}"


# ---- after links, driven by the created children (not by predictions) ------------------------

def _dep_children(feature, entry, packages):
    """The Feature `children` entries of the packages a package depends on (its predecessors), in
    order. Recovers the package-level links from the plan's row dependencies."""
    by_pkg = {c["package"]: c for c in feature.get("children", [])}
    holder = {rid: pkg["package"] for pkg in packages for rid in pkg["rows"]}
    out = []
    for dep_pkg in _dedup(holder[d] for d in entry["depends"] if holder.get(d) and holder[d] != entry["package"]):
        c = by_pkg.get(dep_pkg)
        if c:
            out.append(c)
    return out


def _local_and_ext(s, children):
    """Split a list of child entries into (home task ids, external after records). A child on another
    machine becomes an `after_ext` record (queues.set_after), named with its machine first and the
    Feature's folder prefix (the C5 example's shape)."""
    home_ids, ext = [], []
    for c in children:
        if c.get("machine"):
            ext.append({"ref": f"{c['machine']}:{s.prefix}-{c['ref']}", "machine": c["machine"],
                        "prefix": s.prefix, "id": c["ref"]})
        else:
            home_ids.append(c["ref"])
    return home_ids, ext


def _ext_key(ext):
    """The identity of an `after_ext` list (its machine+id pairs), for comparing a child's current
    external links to the desired ones. `queues.set_after` adds polling fields (`state`, `checked`) to
    the stored records, so a whole-record comparison would see every rerun as changed and re-query the
    machine; comparing identity keeps a rerun idempotent and avoids needless cross-machine calls."""
    return [(e.get("machine"), e.get("id")) for e in (ext or [])]


def _link_home_children(s, feature, packages):
    """Set each home child's `after` to the children of the packages it depends on, reconciling the
    whole graph (so a dependency an amendment removed is cleared, not left pointing at a cancelled
    Task). A dependency on another machine becomes an external after record.

    The new graph is applied in two passes, clear then set, because `set_after` rejects a loop against
    the stored edges: updating one child while the others still hold their old links could look like a
    cycle even though the work table (already checked acyclic) has none. Clearing first removes every
    obsolete edge before any new one is validated. When the graph already matches, nothing is written,
    so reruns stay idempotent."""
    entry_by_pkg = {e["package"]: e for e in packages}
    home = []
    for c in feature.get("children", []):
        if c.get("machine"):
            continue  # a remote child's after was set on its own machine at creation
        t = s.task_by_id(c["ref"])
        entry = entry_by_pkg.get(c["package"])
        if t and entry:
            home_ids, ext = _local_and_ext(s, _dep_children(feature, entry, packages))
            home.append((t, home_ids, ext))
    if all((t.get("after") or []) == hid and _ext_key(t.get("after_ext")) == _ext_key(ext)
           for t, hid, ext in home):
        return
    for t, _, _ in home:
        if t.get("after") or t.get("after_ext"):
            set_after(s, t, [])
    for t, hid, ext in home:
        if hid or ext:
            set_after(s, t, hid, ext)


def _ensure_verify(s, feature, change, space, start, queued_at):
    """Create the first Verify Task (role verify) that waits for every child, or reconcile the existing
    one's after with the current children (new ones added, cancelled ones dropped). Returns True when a
    Verify Task was created. The Verify Task waits for children on every machine, not only the home
    ones (proposal section 6.11)."""
    home_ids, ext = _local_and_ext(s, feature.get("children", []))
    if feature.get("verify_task"):
        t = s.task_by_id(feature["verify_task"])
        if t and ((t.get("after") or []) != home_ids or _ext_key(t.get("after_ext")) != _ext_key(ext)):
            set_after(s, t, home_ids, ext)
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
    if space:
        t["queue"] = {"space": space}
    if home_ids or ext:
        set_after(s, t, home_ids, ext)
    if start:
        t["state"], t["queued_at"] = "queued", queued_at
    feature["verify_task"] = t["id"]
    s.dirty = True
    return True


# ---- amendment (section 6.7) ----------------------------------------------------------------

def _amend_warnings(s, feature, packages):
    """The warnings an amendment prints (section 6.7): a started child of a removed row, and a running
    child whose owned paths the new table changed (the running child keeps its owns; the Assistant
    decides). A removed child on another machine is named so the Assistant can act on it there."""
    warnings = []
    pkg_by_rows = {frozenset(pkg["rows"]): pkg for pkg in packages}
    all_rows = {r for pkg in packages for r in pkg["rows"]}
    for c in feature.get("children", []):
        rows = set(c.get("rows") or [])
        if rows and not (rows & all_rows):
            t = _child_record(s, c)
            if c.get("machine"):
                warnings.append(f"row(s) {', '.join(sorted(rows))} removed, but child {_ref_of(s, c)} is on "
                                f"{c['machine']}; cancel it there if it has not started")
            elif t and _started(t):
                warnings.append(f"row(s) {', '.join(sorted(rows))} removed, but child {_ref_of(s, c)} has "
                                f"started; it stays")
            continue
        pkg = pkg_by_rows.get(frozenset(c.get("rows") or []))
        t = _child_record(s, c)
        if pkg and t and sorted(t.get("owns") or []) != sorted(pkg["owns"]):
            warnings.append(f"row(s) {', '.join(sorted(rows))}: the table changed owns to "
                            f"{', '.join(pkg['owns'])}, but child {_ref_of(s, c)} keeps {', '.join(t.get('owns') or [])}")
        if pkg and t and _started(t) and pkg["risk"] != t.get("risk"):
            merge = "ask" if pkg["risk"] == "high" else "checks"
            warnings.append(f"row(s) {', '.join(sorted(rows))}: the table changed risk to {pkg['risk']}; "
                            f"child {_ref_of(s, c)} has started, so its merge is now {merge}")
    return warnings


def _retained_refs(s, feature, packages):
    """The qualified refs of children whose row the table no longer has but that stay anyway (a started
    child, or one on another machine). The Verify Task keeps waiting for them, so the preview lists them
    too; a cancellable (unstarted, home) child is not listed."""
    all_rows = {r for pkg in packages for r in pkg["rows"]}
    out = []
    for c in feature.get("children", []):
        rows = set(c.get("rows") or [])
        if rows and not (rows & all_rows):
            t = _child_record(s, c)
            if c.get("machine") or (t and _started(t)):
                out.append(_ref_of(s, c))
    return out


def _amend_cancel(s, feature, packages):
    """Cancel an unstarted home child of a row the table no longer has (section 6.7). A started child
    is kept (its warning comes from the plan). A removed child on another machine is left for the
    Assistant to cancel there (its warning names it); this machine never cancels another machine's
    Task on its own. Returns the refs of the cancelled children."""
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
    """True once a Task has ever begun, so fan-out must not cancel or rewrite it. A Task that ran keeps
    its `started` time and its worktree even after it is re-queued (resume, cmd_start), so the current
    state alone is not enough: a re-queued child is state `queued` yet has started. Check the start
    history, then the state, so a never-started child (todo/queued/scheduled) is the only cancellable
    one."""
    if task.get("started") or task.get("worktree"):
        return True
    return task.get("state") not in ("todo", "queued", "scheduled", "cancelled")


def _amend_reconcile_risk(s, feature, packages):
    """On an amendment, bring an existing child's risk and merge policy in line with its row (section
    6.6, contract C2: a high-risk Task uses merge=ask). This applies to a started child too: merge
    policy only gates the merge, so raising it to `ask` keeps a newly-risky change from merging without
    the person, and it does not disturb the running work. Only the owned paths of a running child are
    left unchanged (that is `_amend_warnings`' job)."""
    pkg_by_rows = {frozenset(pkg["rows"]): pkg for pkg in packages}
    for c in feature.get("children", []):
        if c.get("machine"):
            continue
        pkg = pkg_by_rows.get(frozenset(c.get("rows") or []))
        t = s.task_by_id(c["ref"])
        if not pkg or not t:
            continue
        if pkg["risk"] != t.get("risk"):
            t["risk"] = pkg["risk"]
            set_task(t, "merge", pkg["merge"])
            s.dirty = True


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
