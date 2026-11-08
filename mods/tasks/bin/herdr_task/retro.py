"""The retro Task for a Feature (proposal 6.15, contract C1/C4/C7).

A retro runs once for a Feature, after the Feature is `done`. It is read-only with respect to the
repo: the retro agent reads the children's digests (`herdr-task retro-data <ref>`), follows Matt
Pocock's retro skill, and presents improvement candidates in order of severity for the person to
accept or decline. It applies nothing itself; an accepted candidate becomes an ordinary follow-up
Task.

The `tasks.retro` setting decides when it runs: `off` (never), `ask` (the Assistant asks the person
first), `automatic` (the tick starts it by itself when the Feature becomes `done`). The
`tasks.retro_save` setting decides where the report goes (`outside` the repo, `repo` as
`docs/retros/`, or `pr` as a pull request comment). The `tasks.retro_prompt` setting decides the
instructions (`built-in` role template, a skill name, or a file path). The retro agent is the
`retro` role, or the Reviewer's agent/model/effort when `retro.*` is empty.
"""

import os

from . import settings
from .base import now
from .output import say
from .store import open_store
from .templates import FRONT


# Where the retro report is saved (contract C1 `tasks.retro_save`): the words the retro agent gets
# in its brief, one per value. `outside` is the default and leaves the repo untouched.
SAVE_WORDS = {
    "outside": "Save your report outside the repo (do not commit it to the repo).",
    "repo": "Save your report in the repo, under docs/retros/.",
    "pr": "Save your report as a comment on the Feature's pull request.",
}

# Used only when the built-in retro role template (templates/retro.md) is not installed on this
# machine: a short stand-in so the retro still runs. The installed template is the source of truth,
# so the person's edits to it take effect (proposal 6.15).
BUILT_IN_FALLBACK = (
    "Follow the built-in retro instructions: first load the writing-for-agents skill, then follow "
    "Matt Pocock's retro skill. Check its seven categories of the coding agent's environment: "
    "Navigation, Automated checks, Coding standards, Global AGENTS.md, Tool economy, No-ops and "
    "Information access. Present the candidates as cards in the Assistant chat, most severe first, "
    "and cap the list. Change nothing yourself to apply a candidate: no edits, no commits. The "
    "person accepts or declines each one, and an accepted candidate becomes an ordinary follow-up "
    "Task."
)


def retro_template():
    """The built-in retro role instructions: the body of the installed templates/retro.md (the copy
    the person edits, which `hc templates` lists), without its frontmatter. None when it is not
    installed, or installed with no body, so the caller always has instructions (BUILT_IN_FALLBACK):
    an empty brief would leave the retro agent with nothing to do, never the intent."""
    try:
        with open(os.path.join(settings.TEMPLATES, "retro.md")) as f:
            text = f.read()
    except OSError:
        return None
    m = FRONT.match(text)
    return (text[m.end():] if m else text).strip() or None


def _retro_role(s, space=None):
    """The agent pick the retro Task runs with: the `retro` role, else the Reviewer's. When neither
    names an agent, the Reviewer's effective default stands in - the other CLI from the worker, as
    the reviewer a task gets does - so the retro reads with fresh eyes and never silently inherits
    the worker's model or effort."""
    cfg = settings.settings_for(s, space)
    pick = dict(cfg.get("retro") or {})
    if not pick:
        pick = dict(cfg.get("reviewer") or {})
    if not pick.get("agent") and not pick.get("argv"):
        # The worker's own CLI is its `agent`, or the command of its `argv` when it is set that way,
        # else claude. The retro runs on the other CLI, so an argv-only worker never makes the retro
        # read with the same CLI as the code it reviews.
        worker = cfg.get("worker") or {}
        worker_cli = worker.get("agent") or (settings.agent_key(worker["argv"]) if worker.get("argv") else "claude")
        pick["agent"] = settings.other_cli(worker_cli)
    return pick


def retro_agent(s, space=None):
    """The retro agent {agent, model, effort} (contract C7): the `retro` role, else the Reviewer's."""
    pick = _retro_role(s, space)
    return {k: pick.get(k) for k in ("agent", "model", "effort")}


def _feature_space(s, feature):
    """The space a Feature's Tasks run in (the spec Task's, else any child's on this machine), or
    None. The Feature record has no space of its own; its Tasks carry it."""
    spec = feature.get("spec_task")
    tasks = s.data.get("tasks") or []
    if spec:
        t = next((t for t in tasks if t.get("id") == spec and t.get("space")), None)
        if t:
            return t["space"]
    for t in tasks:
        if t.get("feature") == feature.get("id") and t.get("space"):
            return t["space"]
    return None


def _instructions(which):
    """The retro instructions for the Task's brief (contract C1 `tasks.retro_prompt`): the built-in
    role template loaded in full (so its edits take effect), a named skill, or a file path."""
    which = (which or "built-in").strip()
    if which == "built-in":
        return retro_template() or BUILT_IN_FALLBACK
    if "/" in which or which.startswith("~") or which.startswith(".") or which.endswith(".md"):
        return f"Follow the retro instructions in {which}."
    return f"Follow the `{which}` skill for the retro."


def _prompt(ref, title, instructions, save):
    """The retro Task's prompt: self-contained, so it works whether or not the brief carries a flow
    file. It names the Feature and the data command, carries the instructions in full, and resolves
    the save target the instructions refer to."""
    return "\n\n".join([
        f"You run the retro for Feature {ref} ({title}), now that it is done. You run once for the "
        "whole Feature, not once for each Child Task. You are read-only: you present improvement "
        "candidates, you never apply them.",
        f"Run `herdr-task retro-data {ref}` for every Child Task's digest, plus the state of the "
        "repo check script and CI. Read the digests first; read a raw transcript only for a child "
        "the digests flag (`hc file read --on <machine> <transcript>`).",
        instructions,
        SAVE_WORDS.get(save, SAVE_WORDS["outside"]),
    ])


def start(s, feature, mode=None):
    """Create the retro Task for a Feature with the retro agent and the `retro_save` target
    (contract C7). Returns the Task, or None when `tasks.retro` is `off` and `mode` was not forced:
    `mode` lets a caller (the `retro` command) force a run even when the setting is `off`.

    The Task is queued with the retro agent's full argv, so the ordinary queue starts it on the
    retro agent and never inherits the worker's model or effort. Review and proof are off: the retro
    applies nothing to the repo, so there is nothing to review and the report itself is its proof. A
    `repo` save gets a worktree so its docs/retros/ commit becomes a pull request; any other save
    works in the folder itself, since it touches nothing there."""
    space = _feature_space(s, feature)
    cfg = settings.settings_for(s, space)["tasks"]
    setting = cfg.get("retro") if cfg.get("retro") in settings.RETRO_WORDS else "off"
    effective = mode or setting
    if effective == "off":
        return None
    which = cfg.get("retro_prompt") or "built-in"
    save = cfg.get("retro_save") if cfg.get("retro_save") in SAVE_WORDS else "outside"
    ref = feature.get("ref") or s.ref(feature)
    title = feature.get("title") or ref
    t = s.new(f"Retro: {title}", _prompt(ref, title, _instructions(which), save),
              mode="worktree" if save == "repo" and settings.worktrees_on(s, space) else "here",
              feature=feature["id"], role="retro")
    pick = _retro_role(s, space)
    for k in ("agent", "model", "effort"):
        if pick.get(k):
            t[k] = pick[k]
    if space:
        t["space"] = space
    # Read-only: no review round (nothing is applied), no proof artifact (the report is the proof).
    settings.set_task(t, "review", "off")
    settings.set_task(t, "proof", "off")
    # Queued with the retro agent's own argv, so the queue starts it on that agent exactly, without
    # inheriting the worker role's model or effort (which worker_argv would otherwise pull in).
    plan = {"argv": settings.argv_for(pick)}
    if space:
        plan["space"] = space
    t["state"], t["queued_at"], t["queue"] = "queued", now(), plan
    s.dirty = True
    return t


def cmd_retro(args):
    """The `retro` command entry (contract C4/C5): start a retro for a Feature now, with the retro
    agent settings, whatever the `tasks.retro` setting is. Prints `{ok, task, mode, saved_to}`;
    `mode` is the setting value and `saved_to` the save target."""
    with open_store(args) as s:
        feature = s.find_feature(args.id)
        space = _feature_space(s, feature)
        cfg = settings.settings_for(s, space)["tasks"]
        setting = cfg.get("retro") if cfg.get("retro") in settings.RETRO_WORDS else "off"
        save = cfg.get("retro_save") if cfg.get("retro_save") in SAVE_WORDS else "outside"
        # The command starts a retro at any time (proposal 6.15), so it forces a run even when the
        # setting is `off`; the queue then starts the Task with the retro agent.
        t = start(s, feature, mode="automatic")
        ref = s.ref(t) if t else None
        feat_ref = feature.get("ref") or s.ref(feature)
    data = {"ok": True, "task": ref, "mode": setting, "saved_to": save}
    say(args, data, f"{ref} queued: the retro for {feat_ref}, saving {save}.")
