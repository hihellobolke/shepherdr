"""The retro Task for a Feature (proposal 6.15, contract C1/C4/C7).

A retro runs once for a Feature, after the Feature is `done`. It is read-only: the retro agent
reads the children's digests (`herdr-task retro-data <ref>`), follows Matt Pocock's retro skill,
and presents improvement candidates in order of severity for the person to accept or decline. It
changes nothing in the repo; an accepted candidate becomes an ordinary follow-up Task.

The `tasks.retro` setting decides when it runs: `off` (never), `ask` (the Assistant asks the person
first), `automatic` (the tick starts it by itself when the Feature becomes `done`). The
`tasks.retro_save` setting decides where the report goes (`outside` the repo, `repo` as
`docs/retros/`, or `pr` as a pull request comment). The `tasks.retro_prompt` setting decides the
instructions (`built-in` role template, a skill name, or a file path). The retro agent is the
`retro` role, or the Reviewer's agent/model/effort when `retro.*` is empty.
"""

from .base import now
from .output import say
from .settings import RETRO_WORDS, settings_for, set_task, worktrees_on
from .store import open_store


# Where the retro report is saved (contract C1 `tasks.retro_save`): the words the retro agent gets
# in its brief, one per value. `outside` is the default and leaves the repo untouched.
SAVE_WORDS = {
    "outside": "Save your report outside the repo (do not commit it to the repo).",
    "repo": "Save your report in the repo, under docs/retros/.",
    "pr": "Save your report as a comment on the Feature's pull request.",
}


def retro_agent(s, space=None):
    """The retro agent {agent, model, effort} (contract C7): the `retro` role when it is set, else
    the Reviewer's. A value the chosen role does not set is None, so the retro Task falls back to
    the worker default (claude) for it, exactly as any task with no agent set does."""
    cfg = settings_for(s, space)
    role = cfg.get("retro") or {}
    if not role:
        role = cfg.get("reviewer") or {}
    return {k: role.get(k) for k in ("agent", "model", "effort")}


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


def _instructions_line(which):
    """The brief line that points the retro agent at its instructions (contract C1
    `tasks.retro_prompt`): the built-in role template, a named skill, or a file path."""
    which = (which or "built-in").strip()
    if which == "built-in":
        return ("Follow the built-in retro instructions (the retro role template): first load the "
                "writing-for-agents skill, then follow Matt Pocock's retro skill. Check its seven "
                "categories of the coding agent's environment: Navigation, Automated checks, Coding "
                "standards, Global AGENTS.md, Tool economy, No-ops and Information access.")
    if "/" in which or which.startswith("~") or which.startswith(".") or which.endswith(".md"):
        return f"Follow the retro instructions in {which}."
    return f"Follow the `{which}` skill for the retro."


def _prompt(ref, title, which, save):
    """The retro Task's prompt: self-contained, so it works whether or not the brief carries a flow
    file. It names the Feature, the data command, the instructions source and the save target."""
    return "\n".join([
        f"You run the retro for Feature {ref} ({title}), now that it is done.",
        "You run once for the whole Feature, not once for each Child Task. You are read-only: you "
        "present improvement candidates, you never apply them.",
        f"Run `herdr-task retro-data {ref}` for every Child Task's digest, plus the state of the "
        "repo check script and CI. Read the digests first; read a raw transcript only for a child "
        "the digests flag (`hc file read --on <machine> <transcript>`).",
        _instructions_line(which),
        "Present the candidates as cards in the Assistant chat, most severe first, and cap the "
        "list: a few strong candidates beat a long noisy one. Each card names the problem, the "
        "evidence, and the one change you suggest. Change nothing yourself: no edits, no commits, "
        "no new files to apply a candidate. The person accepts or declines each one, and an "
        "accepted candidate becomes an ordinary follow-up Task.",
        SAVE_WORDS.get(save, SAVE_WORDS["outside"]),
    ])


def start(s, feature, mode=None):
    """Create the retro Task for a Feature with the retro agent and the `retro_save` target
    (contract C7). Returns the Task, or None when `tasks.retro` is `off` and `mode` was not forced:
    `mode` lets a caller (the `retro` command) force a run even when the setting is `off`.

    The Task is read-only and queued, so the ordinary queue starts it with the retro agent. Review
    and proof are off: the retro changes nothing in the repo, so there is nothing to review and the
    report itself is its proof. A `repo` save gets a worktree so its docs/retros/ commit becomes a
    pull request; any other save works in the folder itself, since it touches nothing there."""
    space = _feature_space(s, feature)
    cfg = settings_for(s, space)["tasks"]
    setting = cfg.get("retro") if cfg.get("retro") in RETRO_WORDS else "off"
    effective = mode or setting
    if effective == "off":
        return None
    which = cfg.get("retro_prompt") or "built-in"
    save = cfg.get("retro_save") if cfg.get("retro_save") in SAVE_WORDS else "outside"
    ref = feature.get("ref") or s.ref(feature)
    title = feature.get("title") or ref
    t = s.new(f"Retro: {title}", _prompt(ref, title, which, save),
              mode="worktree" if save == "repo" and worktrees_on(s, space) else "here",
              feature=feature["id"], role="retro")
    agent = retro_agent(s, space)
    for k in ("agent", "model", "effort"):
        if agent.get(k):
            t[k] = agent[k]
    if space:
        t["space"] = space
    # Read-only: no review round (nothing changes), no proof artifact (the report is the proof).
    set_task(t, "review", "off")
    set_task(t, "proof", "off")
    # Queued, so the ordinary queue starts it with the retro agent when there is room.
    t["state"], t["queued_at"], t["queue"] = "queued", now(), {"space": space} if space else {}
    s.dirty = True
    return t


def cmd_retro(args):
    """The `retro` command entry (contract C4/C5): start a retro for a Feature now, with the retro
    agent settings, whatever the `tasks.retro` setting is. Prints
    `{ok, task, mode, saved_to}`; `mode` is the setting value and `saved_to` the save target."""
    with open_store(args) as s:
        feature = s.find_feature(args.id)
        space = _feature_space(s, feature)
        cfg = settings_for(s, space)["tasks"]
        setting = cfg.get("retro") if cfg.get("retro") in RETRO_WORDS else "off"
        save = cfg.get("retro_save") if cfg.get("retro_save") in SAVE_WORDS else "outside"
        # The command starts a retro at any time (proposal 6.15), so it forces a run even when the
        # setting is `off`; the queue then starts the Task with the retro agent.
        t = start(s, feature, mode="automatic")
        ref = s.ref(t) if t else None
    data = {"ok": True, "task": ref, "mode": setting, "saved_to": save}
    say(args, data, f"{ref} queued: the retro for {feature.get('ref') or s.ref(feature)}, saving {save}.")
