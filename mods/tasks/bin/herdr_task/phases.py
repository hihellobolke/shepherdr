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
- Verify reported a `blocker` finding and no Fix Task is in flight -> attention `blocker-finding`:
  the Assistant could not turn the blocker into a fix, so the loop stops at once (section 6.11).
- Verify reported only non-blocker findings and no Fix Task is in flight -> this is the resting
  state between rounds. `verify-open` means open findings *after the allowed fix rounds* (contract
  C2), so deciding it needs the `verify_rounds` setting, which only `fix_round_allowed` carries.
  `phase_of` takes `verify_rounds` as an optional argument for exactly this: with it, `phase_of`
  returns `verify-open` only once `fix_round_allowed` is false (the rounds are used up), and `""`
  while a round is still allowed (so `tick.py` opens the next one). Called with the bare contract
  signature (`verify_rounds` left out), `phase_of` returns `""` and leaves the `verify-open`
  decision to the caller; it never raises `verify-open` early. When it holds, the person sees Needs
  you with the Add a fix round action (contract C9 keys that action on `verify-open`).

### Several machines (proposal section 6.13)

A child id is unique only within its own machine's store (contract C2), so `blocked_dependents`
identifies a child by `(machine, id)`, never by id alone, and resolves a child's local `after` ids
against that same machine. Chain affinity keeps a chain on one machine, so the dependency links that
matter for blocking are local; cross-machine links between waves (`after_ext`) are not walked here.
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


def _ident(task):
    """A child's ref when it carries one, else its id: how a dependent is named back to the caller."""
    return task.get("ref") if task.get("ref") is not None else task.get("id")


def _key(task):
    """A child's machine-qualified identity `(machine, id)` (contract C2, section 6.13).

    A child id is unique only within its own machine's store, so two machines can both hold a child
    `14`. The machine (None for the home machine) keeps them apart in the dependency graph.
    """
    return (task.get("machine"), task.get("id"))


def blocked_dependents(children):
    """The children a failure blocks, by dependency (`after`), the block propagated down the chain.

    A child is blocked when one of the children it waits on (its local `after` ids, on its own
    machine) is blocking (`is_blocking`) or is itself blocked: a failure with no retry left stops its
    whole chain of dependents, while the other chains keep running (proposal section 6.12). A resolved
    child (merged or skipped) or a cancelled one is never blocked and never passes a block on, so
    skipping a failed row unblocks everything below it: a later call returns the shorter list. Only
    dependents are returned, never the failed child itself.

    Returns the dependents' refs (or ids) in sorted order.
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
            deps = [(c.get("machine"), d) for d in (c.get("after") or [])]
            if any(d in blockers or d in blocked for d in deps):
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


def phase_of(feature, children, verify_rounds=None):
    """The `(phase, attention)` a Feature should have, given its children's states (contract C7).

    Pure: no IO. `feature` is the Feature record (contract C2) and `children` is the list of its
    flow Task records. The returned phase is never past what the children's states allow; the
    transitions this module does not own are preserved from the record (see the module docstring).

    `verify_rounds` is the optional setting (contract C7 `fix_round_allowed`). It is only read in the
    `verifying` phase, to decide `verify-open` (open findings after the allowed fix rounds). Left out,
    the bare contract call never raises `verify-open`; given, `phase_of` raises it once the rounds are
    used up (see the module docstring).
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
        findings = verify_task.get("findings") or []
        if any((f or {}).get("level") == "blocker" for f in findings):
            # A blocker with no fix in flight: the Assistant could not turn it into a fix, so stop.
            return (VERIFYING, ATTENTION_BLOCKER_FINDING)
        if findings and verify_rounds is not None and not fix_round_allowed(feature, verify_rounds):
            return (VERIFYING, ATTENTION_VERIFY_OPEN)  # open findings after the allowed fix rounds
        return (VERIFYING, "")

    return (phase, "")
