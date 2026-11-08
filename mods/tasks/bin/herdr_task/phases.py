"""Phase rules for the OpenSpec flow (T11a).

A Feature is a record with a `phase` and an `attention` reason for Needs you (contract C2). This
module holds the pure rules that decide, from a Feature record and the state of its children, the
phase and attention the Feature should have, whether another fix round is within the setting,
whether a failed or stalled child may take its one retry, and which dependents a failure or a skip
blocks or unblocks. It does no IO: no git, no store writes, no network. `tick.py` (T19) reads the
children's real states and digests, calls the rules here to decide the next phase, and writes it.

The seven phases are the ones of proposal section 2.3 and contract C2:

    planning   The spec Task exists and is not accepted.
    building   Plan approval is done. Children run. The Feature has no agent of its own.
    verifying  All children are merged or skipped. A Verify Task runs.
    closing    Verify is clean. herdr-task runs the closing steps (section 6.11).
    ready      The pull request is open. The person presses Merge feature.
    done       The pull request is merged.
    cancelled  The person cancelled the Feature.

This module owns only two of the forward transitions, the ones that a tick can decide on its own
from the children's states: building to verifying (every child merged or skipped) and verifying to
closing (the Verify Task passed). The others are driven elsewhere and this module preserves them:
planning to building is the Plan approval hook (`feature.on_spec_accepted`, contract C8); closing to
ready is `closing.close`; ready to done is `feature.merge`; any phase to cancelled is
`feature.cancel`. For `closing`, the attention that `closing.close` sets (`row-no-child`,
`base-conflict`) is read back from the record, because those need the work table and git and so
cannot be derived here.

### How a child's state is read (proposal sections 6.11 and 6.12)

A child is a flow Task: an ordinary Task with the flow fields of contract C2 (`role`, `rows`,
`retries`, `skipped`, `verify_pass`, `findings`), plus the base Task fields `id`, `state`, `merged`
and `after`. The rules here read:

- A child is *merged* when its `state` is `done` and its `merged` flag is set.
- A child is *skipped* when its `skipped` flag is set. A skipped row unblocks its dependents
  (section 6.12): a skipped child counts as satisfied, so a dependent may run and the Feature may
  leave `building`.
- A child in an *exception* carries the reason in its `attention` field, one of the child-level
  reasons below; `tick.py` sets it (a stall needs live-agent timing a pure function cannot see). The
  reason maps to the Feature's attention of contract C2:

      failed      -> child-failed
      stalled     -> child-stalled
      needs-plan  -> needs-plan   (a child asked for a plan change)
      needs-owns  -> needs-owns   (a child asked for more owned paths)

  A `failed` or `stalled` child that may still take its one retry (`should_retry`) is not yet a
  Feature attention: `tick.py` restarts it first. Only once the retry is used, or for the
  person-blocking `needs-plan` and `needs-owns`, does the reason surface on the Feature.

### The Verify Task and fix rounds (proposal section 6.11)

The current Verify Task is the child whose `id` is the Feature's `verify_task`. Before it reports,
`verify_pass` is absent. `verify-report --pass` sets `verify_pass` true; `--findings-file` sets it
false and stores the `findings`. The rule in `verifying`:

- Verify passed -> the Feature moves to `closing`.
- A Fix Task of the current round is still in flight -> attention stays `""`: the round is being
  worked, whatever the findings say.
- Verify passed -> `closing`.

Two of the verifying attentions are *not* `phase_of`'s to raise, because deciding them needs an input
`phase_of` does not have, so it leaves them to the caller (`tick.py`, contract C8) and only keeps
them once the caller has set them on the record:

- `verify-open` means open findings *after the allowed fix rounds* (contract C2). Whether the rounds
  are used up needs the `verify_rounds` setting, which the contract gives to `fix_round_allowed`, not
  to `phase_of`. So `tick.py` decides: with a fix round still allowed (`fix_round_allowed`) it opens
  the next one; with the rounds used up it sets `verify-open` and the person sees Needs you with the
  Add a fix round action (contract C9 keys that action on `verify-open`).
- `blocker-finding` means a blocker the Assistant *cannot turn into a fix* (section 6.11). Whether a
  blocker is fixable is the Assistant's judgment, not something the child states show: the mere
  absence of a Fix Task right after a report does not prove it is unfixable (the Assistant may be
  about to create one). So the Assistant/`tick.py` sets `blocker-finding` once it gives up on a fix.

So in the resting state (findings reported, no Fix Task in flight) `phase_of` returns the Feature's
own `verify-open` or `blocker-finding` when the caller has already set it, and `""` otherwise. It
never raises either on its own, and it clears any unrelated stale attention.

### Several machines (proposal section 6.13)

A child id is unique only within its own machine's store (contract C2), so `blocked_dependents`
identifies a child by `(machine, id)`, never by id alone. It walks both a child's local `after` ids
(resolved against that child's own machine) and its cross-machine `after_ext` links between waves
(each already machine-qualified), and it returns each blocked dependent machine-qualified too
(`"<machine>:<ref>"` for a child off the home machine), so the caller can tell two machines' children
with the same ref apart. The home machine is one identity however it is written: a record's `None` and
the digest's `"local"` both mean home (`_norm_machine`), so a link back to home from a remote child
matches the home child. A link that names home by its hostname is the exception a pure module cannot
resolve (it does not know the host's name); the caller normalizes such a link to `None` or `"local"`
before calling.
"""

# The seven phases (contract C2, proposal section 2.3).
PLANNING = "planning"
BUILDING = "building"
VERIFYING = "verifying"
CLOSING = "closing"
READY = "ready"
DONE = "done"
CANCELLED = "cancelled"

PHASES = (PLANNING, BUILDING, VERIFYING, CLOSING, READY, DONE, CANCELLED)

# The attention reason codes for Needs you (contract C2). `""` means no attention.
ATTENTION_VERIFY_OPEN = "verify-open"
ATTENTION_ROW_NO_CHILD = "row-no-child"
ATTENTION_CHILD_FAILED = "child-failed"
ATTENTION_CHILD_STALLED = "child-stalled"
ATTENTION_NEEDS_PLAN = "needs-plan"
ATTENTION_NEEDS_OWNS = "needs-owns"
ATTENTION_BASE_CONFLICT = "base-conflict"
ATTENTION_BLOCKER_FINDING = "blocker-finding"

ATTENTION = (
    ATTENTION_VERIFY_OPEN,
    ATTENTION_ROW_NO_CHILD,
    ATTENTION_CHILD_FAILED,
    ATTENTION_CHILD_STALLED,
    ATTENTION_NEEDS_PLAN,
    ATTENTION_NEEDS_OWNS,
    ATTENTION_BASE_CONFLICT,
    ATTENTION_BLOCKER_FINDING,
)

# Routine child events the Feature only counts; they are not sent to the Assistant one by one
# (proposal section 6.14, contract C8). The exception events are sent one by one.
ROUTINE_EVENTS = ("started", "ready", "merged")
EXCEPTION_EVENTS = (
    "needs-you",
    "failed",
    "stalled",
    "needs-plan",
    "needs-owns",
    "risky-waiting",
    "verify-findings",
)

# The phases that `phase_of` preserves rather than derives. closing keeps the attention that
# `closing.close` set; the three terminal/person-driven phases carry no attention.
_PRESERVED = (PLANNING, CLOSING, READY, DONE, CANCELLED)

# A child-level exception reason maps to the Feature's attention of contract C2.
_CHILD_EXCEPTION = {
    "failed": ATTENTION_CHILD_FAILED,
    "stalled": ATTENTION_CHILD_STALLED,
    "needs-plan": ATTENTION_NEEDS_PLAN,
    "needs-owns": ATTENTION_NEEDS_OWNS,
}

# The child-level reasons that take the one retry before they surface on the Feature.
RETRYABLE = ("failed", "stalled")

# When several children carry an exception, the Feature shows the most urgent, in the order the
# contract (C2) lists the child-exception codes.
_ATTENTION_PRIORITY = (
    ATTENTION_CHILD_FAILED,
    ATTENTION_CHILD_STALLED,
    ATTENTION_NEEDS_PLAN,
    ATTENTION_NEEDS_OWNS,
)

# The states of a child that is still working (not finished, not cancelled).
_WORKING = ("todo", "scheduled", "queued", "running", "ready")


def _is_merged(task):
    return task.get("state") == "done" and bool(task.get("merged"))


def _is_skipped(task):
    return bool(task.get("skipped"))


def _is_cancelled(task):
    return task.get("state") == "cancelled" or bool(task.get("cancelled"))


def is_satisfied(task):
    """True when a child no longer holds up its dependents: it is merged or skipped.

    A skipped row unblocks its dependents (proposal section 6.12), so a skipped child is satisfied
    just as a merged one is.
    """
    return _is_merged(task) or _is_skipped(task)


def should_retry(task):
    """True when a failed or stalled Task may take its one restart (contract C7, section 6.12).

    A failed or stalled child restarts once from its own branch; `retries` counts the restarts used,
    so a child with a `failed` or `stalled` reason and `retries` below 1 may retry. A child that has
    been resolved (merged or skipped) or cancelled never retries: a retry must not undo a skip or a
    cancellation. Any other Task (a different reason, none, or one that has used its retry) may not.
    """
    if is_satisfied(task) or _is_cancelled(task):
        return False
    return task.get("attention") in RETRYABLE and int(task.get("retries") or 0) < 1


def is_blocking(task):
    """True when a child holds up the children that depend on it.

    A child blocks when it failed or stalled and has no retry left (so `tick.py` will not restart
    it). A resolved child (merged or skipped) or a cancelled one never blocks: skipping the failed
    child is exactly how the person unblocks its dependents (section 6.12).
    """
    if is_satisfied(task) or _is_cancelled(task):
        return False
    return task.get("attention") in RETRYABLE and not should_retry(task)


def _norm_machine(machine):
    """One spelling of the home machine (contract C2, section 6.13).

    A record writes the home machine as `None` (the Feature children entries) while the digest keys it
    as `"local"`; both mean home, so they normalize to `None`. A cross-machine link that names home by
    its hostname cannot be recognised here (a pure module does not know the host's name): the caller,
    which knows it, normalizes such a link to `None` or `"local"` before calling (see the docstring).
    """
    return None if machine in (None, "local") else machine


def _key(task):
    """A child's machine-qualified identity `(machine, id)`, home normalized (contract C2, section 6.13).

    A child id is unique only within its own machine's store, so two machines can both hold a child
    `14`. The machine keeps them apart in the dependency graph; the home machine is one key either way
    it is spelled (`_norm_machine`).
    """
    return (_norm_machine(task.get("machine")), task.get("id"))


def _ident(task):
    """A child's machine-qualified name for the caller: `"<machine>:<ref>"`, or just `ref`/`id` at home.

    A child off the home machine is named `"<machine>:<ref>"` (the way Shepherdr writes a cross-machine
    ref, e.g. `vm-b:HAT-15`), so two machines' children with the same ref are told apart. A home-machine
    child is named by its `ref` when it has one, else its `id`.
    """
    name = task.get("ref") if task.get("ref") is not None else task.get("id")
    machine = _norm_machine(task.get("machine"))
    return "%s:%s" % (machine, name) if machine else name


def _dep_keys(task):
    """Every child this task waits on, as home-normalized `(machine, id)` keys (contract C2, 6.13).

    Local links are the `after` ids, which name tasks in this child's own machine's store. Cross-machine
    links between waves are the `after_ext` entries, each carrying its own `machine` and `id` (a link
    back to the home machine normalizes to the same home key as a home child).
    """
    machine = _norm_machine(task.get("machine"))
    keys = [(machine, d) for d in (task.get("after") or [])]
    keys += [(_norm_machine(e.get("machine")), e.get("id")) for e in (task.get("after_ext") or [])]
    return keys


def blocked_dependents(children):
    """The children a failure blocks, by dependency, the block propagated down the chain.

    A child is blocked when one of the children it waits on (its local `after` ids on its own machine,
    or its cross-machine `after_ext` links) is blocking (`is_blocking`) or is itself blocked: a failure
    with no retry left stops its whole chain of dependents, while the other chains keep running
    (proposal section 6.12). A resolved child (merged or skipped) or a cancelled one is never blocked
    and never passes a block on, so skipping a failed row unblocks everything below it: a later call
    returns the shorter list. Only dependents are returned, never the failed child itself.

    Returns the dependents' machine-qualified names (`_ident`) in sorted order.
    """
    by_key = {_key(c): c for c in children if c.get("id") is not None}
    blockers = {_key(c) for c in children if c.get("id") is not None and is_blocking(c)}
    blocked = set()
    changed = True
    while changed:
        changed = False
        for c in children:
            if c.get("id") is None:
                continue
            key = _key(c)
            if key in blocked or key in blockers:
                continue
            if is_satisfied(c) or _is_cancelled(c):
                continue  # a resolved or cancelled child breaks the chain: nothing below it is blocked by this
            if any(d in blockers or d in blocked for d in _dep_keys(c)):
                blocked.add(key)
                changed = True
    return sorted((_ident(by_key[k]) for k in blocked), key=str)


def fix_round_allowed(feature, verify_rounds):
    """True when another fix round is within the `verify_rounds` setting (contract C7).

    A Feature allows `verify_rounds` fix rounds (the setting, default 2). `fix_rounds` counts the
    rounds used, so another round is allowed while `fix_rounds` is below `verify_rounds`. Over it,
    the Feature shows Needs you with the Add a fix round action, which the person can still use.
    """
    return int(feature.get("fix_rounds") or 0) < int(verify_rounds)


def is_routine(event):
    """True for a routine child event (`started`, `ready`, `merged`) the Feature only counts.

    The exception events (`EXCEPTION_EVENTS`) are sent to the Assistant one by one (section 6.14,
    contract C8), so they are not routine.
    """
    return event in ROUTINE_EVENTS


def _child_attention(children):
    """The Feature attention a child's exception calls for, or `""` when none does.

    A `failed` or `stalled` child that may still retry is skipped: `tick.py` restarts it before it
    surfaces. The rest map by `_CHILD_EXCEPTION`, and the most urgent of those present wins
    (`_ATTENTION_PRIORITY`).
    """
    found = set()
    for c in children:
        if is_satisfied(c):
            continue  # a merged or skipped child is resolved: its exception no longer counts
        reason = c.get("attention")
        mapped = _CHILD_EXCEPTION.get(reason)
        if not mapped:
            continue
        if reason in RETRYABLE and should_retry(c):
            continue
        found.add(mapped)
    for a in _ATTENTION_PRIORITY:
        if a in found:
            return a
    return ""


def _current_verify(feature, children):
    """The Feature's current Verify Task: the child whose id is the record's `verify_task`, or None.

    Only the named task counts. An older Verify Task that already reported is not read as the current
    result: when `verify_task` names a task that is not present yet (a fresh round whose Verify Task
    has not arrived), the result is None and the Feature keeps waiting rather than acting on a stale
    pass or stale findings.
    """
    vid = feature.get("verify_task")
    if vid is None:
        return None
    for c in children:
        if c.get("role") == "verify" and c.get("id") == vid:
            return c
    return None


def _verify_reported(verify_task):
    """True once the Verify Task has reported: `verify_pass` is set (true for pass, false for findings)."""
    return verify_task is not None and verify_task.get("verify_pass") is not None


def _fix_in_flight(children):
    """True when a Fix Task of the current round is still working (so the round is not done)."""
    return any(
        c.get("role") == "fix" and c.get("state") in _WORKING and not is_satisfied(c)
        for c in children
    )


def phase_of(feature, children):
    """The `(phase, attention)` a Feature should have, given its children's states (contract C7).

    Pure: no IO. `feature` is the Feature record (contract C2) and `children` is the list of its
    flow Task records. The returned phase is never past what the children's states allow; the
    transitions and the two verifying attentions this module does not own are preserved from the
    record rather than raised here (see the module docstring).
    """
    phase = feature.get("phase") or PLANNING

    if phase == CLOSING:
        return (CLOSING, feature.get("attention") or "")
    if phase in _PRESERVED:
        return (phase, "")
    if phase not in PHASES:
        return (phase, "")

    if phase == BUILDING:
        att = _child_attention(children)
        if att:
            return (BUILDING, att)
        kids = [c for c in children if c.get("role") == "child" and not _is_cancelled(c)]
        if kids and all(is_satisfied(c) for c in kids):
            return (VERIFYING, "")
        return (BUILDING, "")

    if phase == VERIFYING:
        att = _child_attention(children)
        if att:
            return (VERIFYING, att)
        verify_task = _current_verify(feature, children)
        if not _verify_reported(verify_task):
            return (VERIFYING, "")
        if verify_task.get("verify_pass"):
            return (CLOSING, "")
        if _fix_in_flight(children):
            return (VERIFYING, "")  # a fix round is running: wait, whatever the findings say
        if verify_task.get("findings"):
            # The resting state: findings are open with no Fix Task in flight. verify-open (needs the
            # verify_rounds setting) and blocker-finding (needs the Assistant's can-fix judgment) are
            # the caller's to raise; phase_of keeps one the caller has set and clears anything else.
            prev = feature.get("attention") or ""
            return (VERIFYING, prev if prev in (ATTENTION_VERIFY_OPEN, ATTENTION_BLOCKER_FINDING) else "")
        return (VERIFYING, "")

    return (phase, "")
