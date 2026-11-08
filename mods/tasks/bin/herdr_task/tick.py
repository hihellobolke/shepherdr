"""Tick: one pass over the tasks (start queued ones, finish merged ones, retry cut turns, hand a
task whose CLI can't go on to its backup)."""

import fcntl
import json
import os
import secrets
import subprocess
import time

from .base import Fail, guard, hc_json, now, PR_EVERY, run, SHELLS, STORE
from .settings import machine_name, worker_argv
from . import settings as settings_mod
from .agents import agent_log, agent_running, busy_again, detail, held_ask, live_agents, status_word, task_status
from .provider import NUDGE, provider_cut, provider_error, PROVIDER_WAITS
from .sessions import find_lost_logs, refresh_sessions, session_of
from .github import already_merged, finish, github, merged_here, pr_merged, pr_pending, pr_problem, pr_view, watch_main
from .launch import coordinator_in, default_space
from .store import locate, read_list, Store, store_files
from .queues import machine_hold, refresh_ext, running_count, set_hold, waiting_ext, waiting_for
from .archive import archive_pass
from .browsers import browser_pass
from .output import progress_clock, say
from .commands import back_to_work, begin, merge_now, start_again, MERGE_FAILED
from . import news
from .news import news_pass, typing
from .takeover import cant_go_on, takeover
from .ghnews import main_pass, pr_deliver, pr_news
from .scheduler import SCHEDULER
from .revive import revive_pass
from . import phases
from . import fanout
from . import closing
from . import feature_status
from . import digest
from . import retro
from . import feature as feature_mod


def at_work(s, t):
    """Its agent is mid-turn (working, or asking): a task is never marked merged under it (SHEP-23);
    a later tick looks again."""
    return any(status_word(k) in ("working", "blocked") for k in live_agents(s.key(t)))


def facts(s, t, report):
    """A worktree task whose own work merged anywhere is done (once its agent is between turns); a
    pull request's problems need the person."""
    if t.get("pr"):
        pr = t["pr"]
        if now() - pr.get("checked", 0) < PR_EVERY:
            return
        pr["checked"] = now()
        s.dirty = True
        v = pr_view(t)
        if not v:
            return
        if v.get("state") == "MERGED":
            if at_work(s, t):
                return
            t["merged"] = True
            watch_main(s, t, (v.get("mergeCommit") or {}).get("oid"))
            finish(s, t)
            report["done"].append(t["id"])
            return
        problem = pr_problem(v)
        t["_pr_fresh"] = not problem and not pr_pending(v)
        try:
            pr_news(s, t, v, report)
        except Exception as e:  # never stops the tick
            report.setdefault("github_error", []).append(f"{s.ref(t)}: {e}")
        # Kept up to date while it is back at work too (a fix pushed: the problem goes away), so its
        # agent going quiet with the problem still there reads Needs you (on_it()).
        if t["state"] in ("ready", "running") and problem != t.get("attention"):
            if problem:
                t["attention"], t["attention_at"] = problem, now()
                report["attention"].append(t["id"])
            elif t.get("attention", "").startswith(("Checks failing", "It conflicts", "The pull request was closed", MERGE_FAILED)):
                t.pop("attention")
    elif merged_here(s, t) and not at_work(s, t):
        t["merged"] = True
        finish(s, t)
        report["done"].append(t["id"])
    elif t.get("own") and not at_work(s, t) and now() - t.get("gh_checked", 0) >= PR_EVERY:
        # SHEP-59: its pull request squash-merged without the task knowing (opened by its agent), or a
        # Ready one whose work is already in its base: done, as ready would.
        t["gh_checked"] = now()
        s.dirty = True
        gone = already_merged(s, t) if t["state"] == "ready" else squash_merged(s, t)
        if gone is not None:
            t["merged"] = True
            if gone:
                watch_main(s, t, (gone.get("mergeCommit") or {}).get("oid"))
            finish(s, t)
            report["done"].append(t["id"])


def squash_merged(s, t):
    """A Working task's branch whose pull request (opened by its agent) merged at its tip: that
    pull request, recorded on the task, or None."""
    repo = github(s)
    found = pr_merged(t, repo) if repo else None
    if not found or found.get("headRefOid") != (t.get("own") or t.get("head")):
        return None
    t["pr"] = {"number": found["number"], "url": found["url"], "repo": repo}
    return {**t["pr"], "mergeCommit": found.get("mergeCommit")}


def back_pass(s, t, report):
    """A Ready task whose agent took up work again (a message from anyone in its chat) is Working
    again until its next ready (busy_again()); its Assistant hears it (news: working-again)."""
    live = live_agents(s.key(t))
    # Cheap while nothing works: hc ls alone; its records in full (roles) only then.
    if any(status_word(k) == "working" for k in live) and busy_again(t, [detail(k) for k in live]):
        back_to_work(s, t, "its agent is at work again")
        report.setdefault("working_again", []).append(s.ref(t))


def due_pass(s, report):
    """Scheduled tasks whose time has come (SHEP-38), a time passed while the machine was off too, join
    the queue in the order they were due: the queue below starts them (after their --after tasks,
    when there's room), each once."""
    for t in s.data["tasks"]:
        if t["state"] == "scheduled" and (t.get("scheduled_for") or 0) <= now():
            t["state"], t["queued_at"] = "queued", t.get("scheduled_for") or now()
            t.pop("scheduled_for", None)
            s.dirty = True
            report.setdefault("due", []).append(s.ref(t))


def tick_store(s, report):
    """One folder's turn: Ready tasks back at work are Working again, merged tasks become done,
    auto-merges go when they can, and queued tasks start while there's room."""
    for t in s.data["tasks"]:
        if t["state"] == "ready":
            back_pass(s, t, report)
        if t.get("worktree") and t["state"] in ("running", "ready"):
            facts(s, t, report)
            if t["state"] == "ready" and (t.get("pr") or {}).get("news"):
                try:
                    pr_deliver(s, t, report)
                except Exception as e:  # never stops the tick
                    report.setdefault("github_error", []).append(f"{s.ref(t)}: {e}")
            if t["state"] == "ready" and t.get("auto_merge") and t.get("pr") and not t.get("attention") and t.get("_pr_fresh"):
                if not merge_now(s, t):
                    report["done"].append(t["id"])
            t.pop("_pr_fresh", None)
    due_pass(s, report)
    queued = sorted((t for t in s.data["tasks"] if t["state"] == "queued"), key=lambda t: t.get("queued_at") or 0)
    n = running_count(s) if queued else 0
    budget = [3]  # other machines asked this pass, at most
    for t in queued:
        waits = bool(waiting_for(s, t))
        if not waits:
            refresh_ext(s, t, budget=budget)
            waits = bool(waiting_ext(t))
        if waits or n >= s.max_running():
            if t.pop("held", None):  # waits for something else now
                s.dirty = True
            continue
        # Its machine too busy (HERD-40): it, and every one after it, waits with why.
        held = machine_hold()
        if (t.get("held") or None) != held:
            t["held"] = held
            if not held:
                t.pop("held")
            s.dirty = True
        if held:
            continue
        q = t.get("queue") or {}
        try:
            sp = q.get("space") or t.get("space") or default_space()
            a = begin(s, t, q.get("argv") or worker_argv(s, t, sp), sp, q.get("parent"))
        except Fail as e:
            t["attention"] = f"Couldn't start: {e}"
            s.dirty = True
            continue
        n += 1
        report["started"].append({"folder": s.loc["folder"], "id": t["id"], "ref": s.ref(t), "terminal": a["address"]})
    if s.data.get("main_watch"):
        try:
            main_pass(s, report)
        except Exception as e:  # never stops the tick
            report["main_error"] = str(e)


def lost_log(t, a):
    """An agent of task t that ended with no log recorded, not looked for yet (find_lost_logs())."""
    gone = a.get("ended") or (t.get("state") == "done" and (t.get("ended_at") or 0) > (t.get("done_at") or 0))
    return bool(gone and not a.get("log") and not a.get("log_looked"))


def lost_logs_pass(report, folder=None):
    """Finds the log of every ended task agent here whose session no pass saw (a short run), so its
    chat reads back after its terminal is gone (SHEP-26). Cheap once each was looked for."""
    for p in store_files(folder):
        d = read_list(p)
        tasks = (d or {}).get("tasks") or []
        if not d or not os.path.isdir(d.get("folder") or "") or not any(lost_log(t, a) for t in tasks for a in t.get("agents") or []):
            continue
        with Store(locate(d["folder"])) as s:
            for t in s.data["tasks"]:
                if any(lost_log(t, a) for a in t.get("agents") or []):
                    find_lost_logs(s, t)
                    report.setdefault("logs_found", []).append(s.ref(t))


# ---- Done tasks end their agents (as Shepherdr) -----------------------------------------------
# A Done task's worker and reviewer terminals end (their shells too) once the final news went
# and its agent is quiet; their sessions stay recorded on the task, so `resume` brings one back
# (which makes it running again). Ready tasks keep theirs. Only terminals labelled with that
# task's herdr.task key are touched. "ended_at" says a pass found nothing left of it.

DONE_GRACE_MS = 60000          # the done news and the agent's last words go first
DONE_WORKING_MS = 15 * 60000   # an agent still working this long after Done ends anyway
GUARD_MEMORY = 85              # memory in use (%) past which the guard acts (memory_guard())
GUARD_FILE = os.path.join(STORE, ".memory-guard.json")
GUARD_ALERT_EVERY_MS = 30 * 60000


def end_done(s, t, report, pressed=False):
    """Ends what is left of one Done task's agents now, or what may end: a shell at once, an agent
    once it's idle (or working past DONE_WORKING_MS); under memory pressure (pressed) every idle
    one at once. Returns the addresses it ended."""
    done_at = t.get("done_at") or 0
    if not pressed and now() - done_at < DONE_GRACE_MS:
        return []
    me = os.environ.get("HC_TERMINAL_ID")
    live = [detail(k) for k in live_agents(s.key(t))]
    live = [k for k in live if k.get("address") != me and (k.get("labels") or {}).get("herdr.role") != "coordinator"]
    if not live:
        t["ended_at"] = now()
        s.dirty = True
        return []
    refresh_sessions(s, t, live)
    ended = []
    for k in live:
        if agent_running(k) and status_word(k) == "working" and now() - done_at < DONE_WORKING_MS:
            continue
        if quiet_hc("kill", k).returncode == 0:
            ended.append(k)
    if ended:
        report.setdefault("ended", []).append({"task": s.ref(t), "terminals": [k["address"] for k in ended]})
    return ended


def end_done_pass(report, folder=None, pressed=False):
    """Every Done task here with agents left ends them (end_done()). Cheap when nothing's left:
    a task is looked at once after each time it became Done."""
    ended = []
    for p in store_files(folder):
        d = read_list(p)
        tasks = (d or {}).get("tasks") or []
        if not d or not os.path.isdir(d.get("folder") or "") or not any(
                t.get("state") == "done" and (t.get("ended_at") or 0) <= (t.get("done_at") or 1) for t in tasks):
            continue
        with Store(locate(d["folder"])) as s:
            for t in s.data["tasks"]:
                if t.get("state") == "done" and (t.get("ended_at") or 0) <= (t.get("done_at") or 1):
                    ended += end_done(s, t, report, pressed)
    if ended:
        # Ended terminals leave hc's list too (a moment after: hc rm takes finished ones only).
        time.sleep(1)
        for k in ended:
            quiet_hc("rm", k)
    return [k["address"] for k in ended]


def quiet_hc(verb, k):
    """hc kill / rm of a Done task's terminal, asked as its parent: a parent that ends its own
    child has seen the news, so no "exited" note is typed into the Assistant (hc's caller)."""
    guard(["hc"])
    env = dict(os.environ)
    env.pop("HC_TERMINAL_ID", None)
    if k.get("parent"):
        env["HC_TERMINAL_ID"] = k["parent"]
    return subprocess.run(["hc", verb, k["address"]], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def memory_guard(report):
    """Past GUARD_MEMORY% memory in use, before oomd ends everything: idle agents of Done tasks
    end first (whatever their grace), then the person is told (an alert from the scheduler's
    terminal, once per 30 min while it stays high). HERD-40's hold keeps new tasks waiting."""
    from .machine import sample
    h = sample()
    try:
        with open(GUARD_FILE) as f:
            g = json.load(f)
    except (OSError, ValueError):
        g = {}
    if h["memory"] <= GUARD_MEMORY:
        if g:
            os.remove(GUARD_FILE)
        return
    ended = end_done_pass(report, pressed=True)
    if ended:
        h = sample()
    report["memory_guard"] = {"memory": h["memory"], "ended": ended}
    if h["memory"] <= GUARD_MEMORY or now() - (g.get("alerted") or 0) < GUARD_ALERT_EVERY_MS:
        return
    k = hc_json("get", SCHEDULER)
    if not k or k.get("state") != "live":
        return
    agents = sum(1 for x in (hc_json("ls") or {}).get("terminals", []) if "/" not in x.get("address", "/")
                 and x.get("agent") and x.get("agent") not in SHELLS)
    alert = {"id": secrets.token_hex(4), "at": now(), "title": f"Memory at {h['memory']}%",
             "body": (f"Ended {len(ended)} finished agent{'s' if len(ended) != 1 else ''}. " if ended else "")
             + f"{agents} agents still run here. New tasks wait. Close apps or finish tasks before the system ends them all.",
             "about": True, "task": machine_name() or "This computer"}
    if run(["hc", "label", SCHEDULER, "herdr.alert=" + json.dumps(alert, ensure_ascii=False, separators=(",", ":"))]).returncode == 0:
        g["alerted"] = now()
        report["memory_guard"]["alert"] = alert["title"]
    tmp = f"{GUARD_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(g, f)
    os.replace(tmp, GUARD_FILE)


RETRIES = os.path.join(STORE, ".retries.json")
STALE_CUT_MS = 30 * 60000


def retry_one(row, k, log, report):
    """One agent's provider retry: the row it has now (None: no cut). The row is
    {address, role, space, log, cut, tries, reason, due | over, nudged}."""
    addr = k["address"]
    cut = provider_cut(log) if log else None
    if not cut:
        return None
    if not row or row.get("cut") != cut["id"]:
        # One that ended long before Herdr first saw it is left to the person (old news).
        if (not row or not row.get("cut")) and cut.get("at") and now() - cut["at"] > STALE_CUT_MS:
            return {**(row or {}), "address": addr, "log": log, "cut": cut["id"], "stale": True}
        f = provider_error(cut["text"], cut["code"], cut["status"])
        wait = f["passing"] and cut["tries"] <= len(PROVIDER_WAITS) and PROVIDER_WAITS[cut["tries"] - 1]
        labels = k.get("labels") or {}
        row = {"address": addr, "role": labels.get("herdr.role") or "worker", "space": labels.get("herdr.space"),
               "log": log, "cut": cut["id"], "tries": cut["tries"],
               "reason": f"{f['reason']}, still after {cut['tries']} tries" if f["passing"] and not wait else f["reason"],
               "at": now(), **({"due": now() + wait * 60000} if wait else {"over": True})}
        report["retries"].append({"address": addr, "reason": row["reason"], "tries": row["tries"], "over": bool(row.get("over"))})
    # Not into words the person is writing to it (HERD-71): the next round tries again.
    if row.get("due") and row["due"] <= now() and status_word(k) != "working" and not typing(addr, k):
        r = run(["hc", "send", addr, NUDGE.format(row["reason"].lower())])
        if r.returncode == 0:
            row = {**row, "nudged": now()}
            row.pop("due")
    return row


def retry_pass(report):
    """Provider retries for every live coordinator and task worker here: a turn its provider cut
    off (its own log says so) gets "carry on" after 1, 2, 4 and 8 minutes; the fifth cut, or an
    error waiting can't fix, needs the person. Rows live in .retries.json; a task's row is also on
    the task (its "retry"), which its status reads. Reviewers are left alone, as are agents that
    work, ask (a card Herdr holds) or were written to since (their log no longer ends on the cut)."""
    report.setdefault("retries", [])
    try:
        with open(RETRIES) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    rows = {}
    for k in (hc_json("ls", "--label", "herdr.role=coordinator") or {}).get("terminals", []):
        if k.get("state") != "live" or "/" in k["address"] or held_ask(k["address"]):
            continue
        k = detail(k)
        k["labels"] = dict(k.get("labels") or {}, **{"herdr.role": "coordinator"})
        if not agent_running(k) or status_word(k) == "blocked":
            continue
        pid = (k.get("foreground") or {}).get("pid")
        was = old.get(k["address"]) or {}
        log = was.get("log") if was.get("pid") == pid and was.get("log") else (session_of(pid)[1] if pid else None)
        row = retry_one(was or None, k, log, report)
        if row:
            rows[k["address"]] = {**row, "pid": pid}
        elif pid and log:
            rows[k["address"]] = {"address": k["address"], "pid": pid, "log": log}
    for path in store_files():
        d = read_list(path)
        if not d or not os.path.isdir(d.get("folder") or ""):
            continue
        tasks = [t for t in d.get("tasks") or [] if t.get("state") == "running" or t.get("retry")]
        if not tasks:
            continue
        with Store(locate(d["folder"])) as s:
            for t in s.data["tasks"]:
                if t["state"] != "running":
                    if t.pop("retry", None):
                        s.dirty = True
                    continue
                mine = [detail(k) for k in live_agents(s.key(t))]
                want = None
                for k in mine:
                    if (k.get("labels") or {}).get("herdr.role") == "reviewer" or not agent_running(k) or held_ask(k["address"]):
                        continue
                    if not agent_log(t, k["address"]):
                        refresh_sessions(s, t, [k])
                    log = agent_log(t, k["address"])
                    # Blocked waits for the person, unless its log ends on what only the backup gets past.
                    if status_word(k) == "blocked" and not cant_go_on(log):
                        continue
                    b = t.get("backup") or {}
                    if b.get("state") == "started" and b.get("from_address") == k["address"]:
                        continue  # the agent its backup took over from, still ending
                    row = retry_one(old.get(k["address"]), k, log, report)
                    if row and row.get("over") and not row.get("stale"):
                        row = takeover(s, t, k, row, log, report)
                    if row:
                        row["task"] = f"{s.loc['folder']}#{t['id']}"
                        rows[k["address"]] = row
                    if row and not row.get("stale"):
                        want = {x: row[x] for x in ("address", "reason", "tries", "due", "over") if x in row}
                if (t.get("retry") or None) != want:
                    if want:
                        t["retry"] = want
                    else:
                        t.pop("retry", None)
                    s.dirty = True
                progress_clock(s, t, task_status(t, [k for k in mine if agent_running(k)])[0])
    if rows != old:
        tmp = f"{RETRIES}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(rows, f)
        os.replace(tmp, RETRIES)


# ---- the Feature pass (OpenSpec flow, T19) --------------------------------------------------
# Each tick drives every live Feature in a folder (contract C8): it brings its children's states
# and digests to the home machine (feature_status.gather), stores a digest when a child ends, fires
# the fan-out trigger once the spec merge has landed (fanout.run), restarts a failed or stalled
# child once (the queue starts it again), moves the phase through phases.phase_of, counts fix rounds
# and raises Needs you once they run out, runs the closing steps (closing.close), starts a retro when
# a done Feature's setting is automatic (retro.start), and tells the Feature's Assistant the wave and
# phase-change summaries plus the one-by-one exception events (the news module). A classic folder has
# no `features`, so the whole pass is a no-op for it, and a classic Task is never touched.

# The child-level exception reasons tick.py writes into a child's `attention` for phases.phase_of to
# read (the module leaves these to the caller, contract C8). A generic needs-you (the child asked the
# person something that is not a plan or owns request) and a risky Task waiting are news-only events.
CHILD_EXCEPTIONS = ("failed", "stalled", "needs-plan", "needs-owns")

# The flow roles whose Tasks phases.phase_of reads as a Feature's children (the spec Task is not one).
_CHILD_ROLES = ("child", "verify", "fix", "integration")

# The phases in which the children are worth gathering and the phase rules worth running. A Feature in
# planning has no children yet; a ready, done or cancelled one has nothing left for the tick to move.
_ACTIVE_PHASES = (phases.BUILDING, phases.VERIFYING, phases.CLOSING)

# The human-facing phase words, the same labels as core/features.js PHASE_LABEL (contract C9).
PHASE_WORDS = {
    "planning": "Planning", "building": "Building", "verifying": "Verifying",
    "closing": "Closing", "ready": "Ready to merge", "done": "Done", "cancelled": "Cancelled",
}


def _live_feature(d):
    """True when a folder's store list has a Feature the tick still has to drive, so a folder whose
    only work is a Feature in building, verifying or closing (its spec Task already merged, no queued
    Task) is still looked at. A just-done Feature is looked at once more, for the retro decision
    (`retro_handled`), then left alone. A classic folder has no `features`, so this is False for it."""
    for f in d.get("features") or []:
        phase = f.get("phase")
        if f.get("news_pending"):
            return True  # still has news to deliver, whatever its phase
        if phase == phases.CANCELLED:
            continue
        if phase != phases.DONE or not f.get("retro_handled"):
            return True
    return False


def features_tick(s, report):
    """One folder's Features (contract C8). A classic folder has no `features`, so this is a no-op.

    Each Feature is driven on its own; one Feature's trouble is recorded and never stops the others or
    the rest of the tick."""
    for feature in list(s.data.get("features") or []):
        try:
            one_feature(s, feature, report)
        except Exception as e:  # one Feature's trouble never stops the others or the tick
            report.setdefault("feature_error", []).append(f"{s.ref(feature)}: {e}")


def one_feature(s, feature, report):
    """Drive one Feature through this tick (contract C8)."""
    phase_before = feature.get("phase")
    if phase_before == phases.CANCELLED:
        return
    if phase_before == phases.DONE:
        _maybe_retro(s, feature, report)
        _flush_news(s, feature, report)
        return

    gathered = _gather(s, feature, report) if phase_before in _ACTIVE_PHASES else {"children": []}
    _store_digests(s, feature, gathered)

    # The fan-out trigger: once the Plan is approved (building), fan-out is on for this Feature, and
    # the approved work table has landed on the feature branch, turn it into child Tasks. It runs while
    # the Verify Task is not there yet, because `fanout.run` creates the children first and the Verify
    # Task last (`fanout._ensure_verify`): a run that stopped part way (a child made, then a crash)
    # leaves no Verify Task, so a later tick runs `fanout.run` again and, being idempotent, creates only
    # the children still missing and then the Verify Task. A deferred spec merge simply delays the first
    # run to a later tick, and a tick that fires twice creates no duplicates.
    if (phase_before == phases.BUILDING and feature.get("auto_fanout", True)
            and not feature.get("verify_task") and feature_mod.spec_merged(s, feature)):
        _fanout(s, feature, report)

    children = _flow_children(s, feature, gathered)

    # Count the fix rounds used (contract T19): every Verify Task past the first is one fix round (the
    # Assistant adds a new Verify Task per round, proposal 6.11). The person's Add a fix round sets the
    # same count when it adds its Verify Task, so this never fights it.
    verifies = [t for t in children if t.get("role") == "verify"
                and t.get("state") != "cancelled" and not t.get("cancelled")]
    rounds = max(0, len(verifies) - 1)
    if int(feature.get("fix_rounds") or 0) != rounds:
        feature["fix_rounds"] = rounds
        s.dirty = True

    # Restart a failed or stalled child once, and flag the person-blocking child exceptions, before the
    # phase rules read the children's `attention`.
    _child_pass(s, feature, children, report)

    new_phase, attention = phases.phase_of(feature, children)
    # A Feature cannot verify without a Verify Task. Fan-out creates the children first and the Verify
    # Task last, so a part-done fan-out (some children made, then a failure) can leave every child
    # merged and no Verify Task; without this guard the Feature would move to verifying and strand there
    # (nothing re-runs fan-out, which fires only in building). Hold it in building so the fan-out trigger
    # re-runs and creates the missing Verify Task.
    if new_phase == phases.VERIFYING and not feature.get("verify_task"):
        new_phase, attention = phases.BUILDING, ""
    if new_phase == phases.VERIFYING:
        override = _verify_handling(s, feature, children, report)
        if override is not None:
            attention = override
    _apply_phase(s, feature, new_phase, attention)

    if feature.get("phase") == phases.CLOSING:
        # Re-verify after an Integration Task merges (proposal 6.11; closing.py leaves this to the tick):
        # a base conflict made an Integration Task, and now that it has merged the integrated branch must
        # pass verify again before closing opens the pull request. Otherwise run the closing steps.
        if feature.get("attention") == phases.ATTENTION_BASE_CONFLICT and _integration_merged(s, feature):
            _reverify(s, feature, report)
        else:
            _closing(s, feature, report)

    _waves_pass(s, feature, children, report)
    _remote_exception_news(s, feature, children, report)
    _phase_news(s, feature, phase_before, report)
    _flush_news(s, feature, report)


# ---- gather, digests and fan-out ------------------------------------------------------------

def _gather(s, feature, report):
    """Bring the children's states and digests to the home machine (contract C8). Best effort: a
    failure to reach a machine is recorded and never stops the tick, and a home-only Feature just
    collects its own children."""
    machines = [c.get("machine") for c in feature.get("children") or [] if c.get("machine")]
    try:
        return feature_status.gather(s, feature, machines)
    except Exception as e:  # a bad machine never stops the Feature pass
        report.setdefault("feature_gather_error", []).append(f"{s.ref(feature)}: {e}")
        return {"children": [], "unreachable": machines}


def _store_digests(s, feature, gathered):
    """Write the digest of every child that has ended into `feature.digests` (contract C8, keyed by
    `digest.child_key`). `gather` carries a home child's digest (built by `feature_status.collect`)
    and a remote child's digest (built on its own machine), so both are stored the one way. Each child
    is matched by its machine-qualified key so one machine's digest never lands on another machine's
    child with the same ref (contract C2)."""
    by_key = _gathered_by_key(s, gathered)
    if not by_key:
        return
    digests = feature.setdefault("digests", {})
    for entry in feature.get("children") or []:
        m = _norm_machine(entry.get("machine"))
        c = by_key.get((m, f"{s.prefix}-{entry.get('ref')}"))
        d = (c or {}).get("digest")
        if d and digests.get(digest.child_key(entry)) != d:
            digests[digest.child_key(entry)] = d
            s.dirty = True


def _fanout(s, feature, report):
    """Fire the fan-out trigger (contract C8). `fanout.run` is idempotent, so a tick that fires twice
    creates no duplicates; a bad work table is recorded and the Feature stays in building."""
    try:
        res = fanout.run(s, feature.get("change"), start=True)
    except Fail as e:
        report.setdefault("feature_fanout_error", []).append(f"{s.ref(feature)}: {e}")
        return
    made = (res.get("created") or {}).get("children", 0)
    report.setdefault("feature_fanout", []).append({"feature": s.ref(feature), "created": made})
    _send_feature_news(s, feature, "fanout",
                       f"{s.ref(feature)}: fan-out started {made} child task{'s' if made != 1 else ''}.",
                       report, once=f"fanout:{feature.get('change')}")


# ---- the children the phase rules read ------------------------------------------------------

def _norm_machine(machine):
    """One spelling of the home machine (contract C2): `None` and `"local"` both mean home."""
    return None if machine in (None, "local") else machine


def _gathered_by_key(s, gathered):
    """Map `(machine, ref-string)` -> one child's feature-status entry, from the per-machine gather
    results (contract C5). A child id is unique only within its own machine's store (contract C2), and
    `feature_status` names every child with the Feature's home prefix, so two machines can both hold a
    child the same ref names (`HAT-2`); keying by `(machine, ref)` keeps them apart. The home machine's
    result carries `machine` null. A gather that returns only a flat `children` list (no per-machine
    split) is read as the home machine's, which is the only machine that can collide with itself."""
    out = {}
    results = gathered.get("machines")
    if results:
        for result in results:
            m = _norm_machine(result.get("machine"))
            for c in result.get("children") or []:
                out[(m, c.get("ref"))] = c
    else:
        for c in gathered.get("children") or []:
            out[(None, c.get("ref"))] = c
    return out


def _flow_children(s, feature, gathered):
    """The flow Task records `phases.phase_of` needs, one per Feature child plus the Verify and Fix
    Tasks (contract C7). A home child, Verify, Fix or Integration Task is its real Task record in this
    store. A remote child's Task lives only on its own machine (proposal 6.13), so it is synthesized
    from the gathered feature-status (its live state) and its stored digest (its end state), matched by
    its machine-qualified key so two machines' children with the same ref never collide (contract C2)."""
    records = [t for t in s.data.get("tasks") or []
               if t.get("feature") == feature.get("id") and t.get("role") in _CHILD_ROLES]
    by_key = _gathered_by_key(s, gathered)
    digests = feature.get("digests") or {}
    for entry in feature.get("children") or []:
        m = _norm_machine(entry.get("machine"))
        if m is None:
            continue
        status = by_key.get((m, f"{s.prefix}-{entry.get('ref')}"))
        records.append(_remote_record(entry, status, digests.get(digest.child_key(entry))))
    return records


def _remote_record(entry, status, stored_digest=None):
    """A synthetic child record for a remote child, from its gathered status and its digest. The
    record carries only what the phase rules read: role, rows, and the state (`merged`/`skipped`/
    `cancelled`/`failed`/running) the digest or the live state says. When the machine cannot be reached
    this tick (no current status), the stored digest already carried home (`feature.digests`, contract
    C2) stands in, so a child that merged before its machine went offline stays merged and does not fall
    back to a stale `running` that would hold the Feature up. A remote child cannot be restarted from the
    home machine, so a failed one is marked with its retry already used (`retries` 1): the phase rules
    then surface it as `child-failed` at once rather than waiting for a retry that never comes (remote
    child restart is deferred with remote child creation, proposal 6.13)."""
    rec = {"id": entry.get("ref"), "machine": entry.get("machine"),
           "role": entry.get("role") or "child", "rows": list(entry.get("rows") or [])}
    dstate = ((status or {}).get("digest") or stored_digest or {}).get("state")
    live_state = (status or {}).get("state")
    if dstate == "merged":
        rec["state"], rec["merged"] = "done", True
    elif dstate == "skipped":
        rec["state"], rec["skipped"] = live_state or "running", True
    elif dstate == "cancelled":
        rec["state"], rec["cancelled"] = "cancelled", True
    elif dstate == "failed":
        rec["state"], rec["attention"], rec["retries"] = "failed", "failed", 1
    else:
        rec["state"] = live_state or "running"
    return rec


# ---- the child exception pass (retries, attention, one-by-one events) ------------------------

def _child_pass(s, feature, children, report):
    """Set each home child's flow exception (contract C8), restart a failed or stalled one once, and
    send the one-by-one events (proposal 6.14). Only the work-package children (role `child`) take a
    retry or raise a child exception; the Verify and Fix Tasks are read by the phase rules instead. A
    remote child is restarted on its own machine, not here."""
    for t in children:
        if t.get("machine") is not None or t.get("role") != "child":
            continue
        if phases.is_satisfied(t) or t.get("state") in ("done", "cancelled"):
            _clear_flow_attention(s, t)
            continue
        live = [detail(k) for k in live_agents(s.key(t))]
        ev = _child_exception(s, t, live)
        if ev in ("failed", "stalled") and int(t.get("retries") or 0) < 1:
            _retry_child(s, feature, t, ev, live, report)
        elif ev in CHILD_EXCEPTIONS:
            if t.get("attention") != ev:
                t["attention"] = ev
                s.dirty = True
            _child_news(s, feature, t, ev, report, once=f"{ev}:{t['id']}")
        elif ev == "needs-you":
            _child_news(s, feature, t, "needs-you", report, once=f"needs-you:{t['id']}")
        else:
            _clear_flow_attention(s, t)
        if t.get("risk") == "high" and t.get("state") == "ready" and not t.get("merged"):
            _child_news(s, feature, t, "risky-waiting", report, once=f"risky:{t['id']}")


def _clear_flow_attention(s, t):
    """Drop a flow exception once the child has moved on, without touching a classic attention (a
    pull-request problem `facts` set)."""
    if t.get("attention") in CHILD_EXCEPTIONS:
        t.pop("attention", None)
        s.dirty = True


def _child_exception(s, t, live):
    """The flow exception a home child is in now, or None (contract C8). `failed` and `stalled` come
    from the same reading the Assistant's news uses (`news.news_state`, `stall_of`); a stall needs the
    live-agent timing a pure rule cannot see. `needs-owns` and `needs-plan` are read from the child's
    own status line (the worker sets it to "needs owns change" or "needs plan change" and stops); any
    other person-blocking wait is a generic `needs-you`."""
    running = [k for k in live if agent_running(k)]
    v, event, stall = news.news_state(s, t, running)
    if event == "failed":
        return "failed"
    if stall:
        return "stalled"
    if v.get("status") == "needs":
        text = ((t.get("progress") or {}).get("text") or "").lower()
        if "owns" in text:
            return "needs-owns"
        if "plan" in text or "contract" in text:
            return "needs-plan"
        return "needs-you"
    return None


# What a restarted child hears, so its new agent knows why it was restarted and carries on from its
# own branch (the resume path adds it after the session handoff).
RETRY_NOTE = ("Herdr restarted you because your task {0} without finishing. Your branch and its commits "
              "are still here. Pick up where you left off, run the checks, and say ready.")


def _retry_child(s, feature, t, ev, live, report):
    """Restart a failed or stalled child once, from its own branch (contract C8, proposal 6.12).

    It goes through the existing resume path (`commands.start_again`), which resumes the child's own
    session (or hands off to a fresh agent of the same kind) with a note that says why it was
    restarted, so the restart keeps the child's context and never starts from the bare brief. A stalled
    child's agent is still alive, so it is ended first; if that kill is refused, the restart is left for
    the next tick and the one retry is not spent, so two workers never run in the same checkout."""
    for k in live:
        if k.get("address") and run(["hc", "kill", k["address"]]).returncode != 0:
            report.setdefault("feature_retry_blocked", []).append(s.ref(t))
            return
    space = _feature_space(s, feature)
    t["retries"] = int(t.get("retries") or 0) + 1
    t.pop("attention", None)
    t["state"] = "running"
    try:
        start_again(s, t, space, message=RETRY_NOTE.format("failed" if ev == "failed" else "stalled"))
    except Exception as e:  # a restart that cannot launch is left for the next tick
        t["retries"] -= 1
        report.setdefault("feature_retry_error", []).append(f"{s.ref(t)}: {e}")
        return
    s.dirty = True
    report.setdefault("feature_retry", []).append({"ref": s.ref(t), "event": ev, "retries": t["retries"]})


# ---- verify, fix rounds and the Needs-you after the rounds ----------------------------------

def _verify_handling(s, feature, children, report):
    """The verifying-phase attention tick.py owns (contract C8, proposal 6.11). Returns the attention
    to set, or None to keep what `phases.phase_of` returned.

    When the current Verify Task reports findings, the Assistant is told once (the `verify-findings`
    event) so it adds the Fix Tasks. While a fix round is still allowed (`fix_round_allowed`) the
    Feature waits for that round; once the rounds are used up it shows Needs you with `verify-open`,
    which offers the person the Add a fix round action (contract C9)."""
    verify = _current_verify(feature, children)
    if verify is None or verify.get("verify_pass") is None or verify.get("verify_pass"):
        return None
    findings = verify.get("findings") or []
    if findings:
        _send_feature_news(s, feature, "verify-findings",
                           f"{s.ref(feature)}: verify found {len(findings)} issue{'s' if len(findings) != 1 else ''}.",
                           report, once=f"verify-findings:{verify.get('id')}")
    if _fix_in_flight(children):
        return None
    if findings and not phases.fix_round_allowed(feature, _verify_rounds(s, feature)):
        return phases.ATTENTION_VERIFY_OPEN
    return None


def _current_verify(feature, children):
    """The Feature's current Verify Task (the child whose id is `verify_task`), or None."""
    vid = feature.get("verify_task")
    if vid is None:
        return None
    return next((c for c in children if c.get("role") == "verify" and c.get("id") == vid), None)


def _fix_in_flight(children):
    """True when a Fix Task is still being worked, so the current round is not done yet."""
    return any(c.get("role") == "fix" and c.get("state") in ("todo", "scheduled", "queued", "running", "ready")
               and not phases.is_satisfied(c) for c in children)


def _verify_rounds(s, feature):
    """The `tasks.verify_rounds` setting for this Feature's space (the fix rounds it allows)."""
    try:
        return int(settings_mod.settings_for(s, _feature_space(s, feature))["tasks"].get("verify_rounds") or 2)
    except Exception:
        return 2


# ---- closing, the integration re-verify, and retro ------------------------------------------

def _closing(s, feature, report):
    """Run the closing steps while the Feature is in closing (contract C8). closing.close is
    idempotent and sets its own attention (`row-no-child`, `base-conflict`); a hard error is recorded
    and never stops the tick."""
    try:
        closing.close(s, feature)
    except Fail as e:
        report.setdefault("feature_closing_error", []).append(f"{s.ref(feature)}: {e}")
        return
    report.setdefault("feature_closing", []).append(
        {"feature": s.ref(feature), "phase": feature.get("phase"), "attention": feature.get("attention", "")})


def _integration_merged(s, feature):
    """True when a base-conflict's Integration Task has merged and none is still open. closing made the
    Integration Task on a base conflict (`closing.make_integration_task`); once it merges, the integrated
    branch must be verified again before closing proceeds."""
    integ = [t for t in s.data.get("tasks") or []
             if t.get("feature") == feature.get("id") and t.get("role") == "integration"]
    if not integ:
        return False
    if any(t.get("state") not in ("done", "cancelled") and not t.get("skipped") for t in integ):
        return False  # one is still being worked
    return any(t.get("state") == "done" and t.get("merged") for t in integ)


def _reverify(s, feature, report):
    """Run verify again after an Integration Task merged (proposal 6.11). A fresh Verify Task is made on
    the integrated feature branch and the Feature goes back to verifying with its base-conflict cleared,
    so the pull request is never opened on a branch whose conflict resolution was never verified."""
    title = feature.get("title") or s.ref(feature)
    verify = s.new(f"Verify {s.ref(feature)}: {title}",
                   f"Run /opsx:verify {feature.get('change')} on the feature branch "
                   f"{feature.get('branch')} after the integration merge. You are read-only: do not "
                   f"change code. Report with `herdr-task verify-report --pass` or `--findings-file`.",
                   mode="worktree", feature=feature["id"], role="verify", owns=[])
    verify["base"], verify["base_set"] = feature["branch"], True
    verify["branch"] = f"task/{s.ref(feature)}-{s.ref(verify)}-verify"
    space = _feature_space(s, feature)
    if space:
        verify["queue"] = {"space": space}
    verify["state"], verify["queued_at"] = "queued", now()
    feature["verify_task"] = verify["id"]
    feature["phase"], feature["attention"] = phases.VERIFYING, ""
    s.dirty = True
    report.setdefault("feature_reverify", []).append({"feature": s.ref(feature), "verify_task": s.ref(verify)})
    _send_feature_news(s, feature, "phase",
                       f"{s.ref(feature)} back to Verifying: the integration merged, verifying it again.",
                       report)


def _maybe_retro(s, feature, report):
    """Start a retro when a done Feature's `tasks.retro` is automatic (contract C8). It runs once; the
    `ask` mode is the Assistant's to offer, not the tick's. The decision is marked `retro_handled` so
    a done Feature is not scanned again every tick, whatever the setting."""
    if feature.get("retro_handled"):
        return
    try:
        mode = settings_mod.settings_for(s, _feature_space(s, feature))["tasks"].get("retro")
    except Exception:
        mode = "off"
    if mode == "automatic":
        t = retro.start(s, feature)
        if t is not None:
            report.setdefault("feature_retro", []).append(s.ref(feature))
    feature["retro_handled"] = True
    s.dirty = True


# ---- phase write, waves and news ------------------------------------------------------------

def _apply_phase(s, feature, phase, attention):
    """Write the phase and attention a tick decided (contract C8). The phase must be one of the seven
    (`phases.PHASES`); an unknown one is a programming error and is left as it was."""
    if phase not in phases.PHASES:
        return
    if feature.get("phase") != phase or feature.get("attention", "") != attention:
        feature["phase"] = phase
        feature["attention"] = attention
        s.dirty = True


def _waves_pass(s, feature, children, report):
    """Tell the Assistant when a wave ends: one digest message when every child of a wave is merged or
    skipped (proposal 6.14). A wave is the leading number of a child's row id: the work-table grammar
    defines a row id as "a wave number, a dot, a row number" (contract C3), so `2.1` is wave 2 and the
    id prefix is the authored wave the person labelled the rows with. This is deliberately the authored
    wave, not the topological depth `worktable.waves` recomputes: the summary speaks of the waves the
    person wrote. Each wave is told once (`waves_told`)."""
    waves = {}
    for c in children:
        if c.get("role") != "child":
            continue
        for rid in c.get("rows") or []:
            waves.setdefault(str(rid).split(".", 1)[0], []).append(c)
    told = feature.setdefault("waves_told", [])
    for w in sorted(waves, key=lambda x: int(x) if x.isdigit() else 0):
        kids = waves[w]
        if w not in told and kids and all(phases.is_satisfied(c) for c in kids):
            told.append(w)
            s.dirty = True
            _send_feature_news(s, feature, "wave",
                               f"{s.ref(feature)}: wave {w} done, {len(kids)} task{'s' if len(kids) != 1 else ''} "
                               f"merged or skipped.", report)


def _remote_exception_news(s, feature, children, report):
    """Queue the one-by-one event for a remote child that failed (proposal 6.14). A remote child is not
    restarted or flagged by `_child_pass` (it lives on another machine), but its failure still reaches
    the Assistant, deduped by machine and ref so it is sent once. A remote child off the home machine is
    named `"<machine>:<ref>"`, the way Shepherdr writes a cross-machine ref."""
    for c in children:
        m = c.get("machine")
        if m is None or c.get("attention") != "failed":
            continue
        ref = f"{m}:{s.prefix}-{c.get('id')}"
        _send_feature_news(s, feature, "failed",
                           f"{ref} failed on {m} and cannot be restarted from here.",
                           report, once=f"failed:{m}:{c.get('id')}")


def _phase_news(s, feature, phase_before, report):
    """Tell the Assistant each phase change, one message (proposal 6.14). Sent only when the phase
    actually moved this tick, so it is told once."""
    phase_now = feature.get("phase")
    if phase_now == phase_before:
        return
    _send_feature_news(s, feature, "phase",
                       f"{s.ref(feature)} moved to {PHASE_WORDS.get(phase_now, phase_now)}: "
                       f"{feature.get('title', '')}", report)


def _feature_space(s, feature):
    """The space a Feature's Tasks run in (the spec Task's, else any child's on this machine), or
    None. The Feature record has no space of its own; its Tasks carry it."""
    spec = s.task_by_id(feature.get("spec_task")) if feature.get("spec_task") else None
    if spec and spec.get("space"):
        return spec["space"]
    for t in s.data.get("tasks") or []:
        if t.get("feature") == feature.get("id") and t.get("space"):
            return t["space"]
    return None


def _child_news(s, feature, t, event, report, once=None):
    """One one-by-one child event for the Assistant (proposal 6.14)."""
    title = " ".join(str(t.get("title") or "").split())
    text = {
        "failed": f"{s.ref(t)} failed and could not be restarted: {title}",
        "stalled": f"{s.ref(t)} stalled and could not be restarted: {title}",
        "needs-plan": f"{s.ref(t)} asks for a plan change: {title}",
        "needs-owns": f"{s.ref(t)} asks for more owned paths: {title}",
        "needs-you": f"{s.ref(t)} needs you: {title}",
        "risky-waiting": f"{s.ref(t)} is a risky task waiting for your Accept: {title}",
    }[event]
    _send_feature_news(s, feature, event, text, report, once=once)


def _feature_news_line(ref, event, text):
    """One tagged news line for the Assistant, the `<herdr-news>` shape the chat recognises, keyed by
    `feature` instead of `task` (proposal 6.14, contract C8)."""
    def attr(x):
        return (str(x).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))
    body = text.replace("<", "‹").replace(">", "›")
    return f'<herdr-news feature="{attr(ref)}" event="{event}">{body}</herdr-news>'


def _send_feature_news(s, feature, event, text, report, once=None):
    """Queue one line for the Feature's Assistant (proposal 6.14). It is not delivered here: it is put
    on the Feature's own outbox (`news_pending`) and `_flush_news` delivers it, so a moment with no live
    Assistant, or a send that cannot go now, does not lose the event - a later tick delivers it. `once`
    dedupes a repeating event across ticks (stored under `feature.told`), so the same event is queued
    only once, whatever happens to its delivery."""
    if once is not None:
        told = feature.setdefault("told", {})
        if told.get(once):
            return
        told[once] = now()
    feature.setdefault("news_pending", []).append({"event": event, "text": text, "at": now()})
    s.dirty = True
    report.setdefault("feature_news", []).append({"feature": s.ref(feature), "event": event, "text": text})


def _flush_news(s, feature, report):
    """Deliver the Feature's queued news to its Assistant, keeping an item that cannot be delivered now
    for a later tick (proposal 6.14). An item is dropped only once it is delivered or has waited longer
    than the task news keeps an undelivered line (`news.NEWS_KEEP_MS`). Delivery is held while the person
    is writing to the Assistant, the way the task news is (`news.send_or_hold`)."""
    pending = feature.get("news_pending")
    if not pending:
        return
    space = _feature_space(s, feature)
    to = coordinator_in(space) if space else None
    kept = []
    for item in pending:
        if now() - (item.get("at") or 0) > news.NEWS_KEEP_MS:
            continue  # too old to still be news: drop it
        delivered = False
        if to:
            line = _feature_news_line(s.ref(feature), item["event"], item["text"])
            try:
                delivered = bool(news.send_or_hold(to, line, how="news"))
            except Exception as e:  # a bad send never stops the Feature pass
                report.setdefault("feature_news_error", []).append(str(e))
        if delivered:
            report.setdefault("feature_news_sent", []).append(
                {"feature": s.ref(feature), "event": item["event"]})
        else:
            kept.append(item)
    if kept != pending:
        feature["news_pending"] = kept
        s.dirty = True


def cmd_tick(args):
    """Starts queued tasks that can go now and finds merged ones. Cheap when nothing waits; one
    at a time (a second tick while one runs does nothing)."""
    os.makedirs(STORE, exist_ok=True)
    lock = open(os.path.join(STORE, ".tick.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say(args, {"busy": True}, "Another tick is running.")
        return
    report = {"started": [], "done": [], "attention": [], "fired": [], "retries": []}
    if getattr(args, "machine", None) or getattr(args, "hold", None):
        try:
            set_hold(args.machine, json.loads(args.hold) if args.hold else None)
        except ValueError:
            pass
    try:
        # First: an Assistant or worker the core gave a plain shell (a crash, a reboot) gets its agent back.
        revive_pass(report)
    except Exception as e:  # never stops the queue
        report["revive_error"] = str(e)
    try:
        retry_pass(report)
    except Exception as e:  # never stops the queue
        report["retry_error"] = str(e)
    try:
        archive_pass(report, args.folder)
    except Exception as e:  # never stops the queue
        report["archive_error"] = str(e)
    for p in store_files(args.folder):
        d = read_list(p)
        tasks = (d or {}).get("tasks") or []
        if not d or not os.path.isdir(d.get("folder") or "") or not (d.get("main_watch") or _live_feature(d) or any(
                t.get("state") in ("queued", "scheduled", "ready") or (t.get("worktree") and t.get("state") == "running") for t in tasks)):
            continue
        with Store(locate(d["folder"])) as s:
            tick_store(s, report)
            try:
                features_tick(s, report)
            except Exception as e:  # the Feature pass never stops the classic tick
                report.setdefault("feature_error", []).append(f"{os.path.basename(s.loc['folder'])}: {e}")
    try:
        news_pass(report, args.folder)
    except Exception as e:  # never stops the queue
        report["news_error"] = str(e)
    try:
        end_done_pass(report, args.folder)
        memory_guard(report)
    except Exception as e:  # never stops the queue
        report["ended_error"] = str(e)
    try:
        lost_logs_pass(report, args.folder)
    except Exception as e:  # never stops the queue
        report["logs_error"] = str(e)
    try:
        browser_pass(report)
    except Exception as e:  # never stops the queue
        report["browser_error"] = str(e)
    say(args, report, "; ".join(f"{k}: {v}" for k, v in report.items() if v) or "Nothing to do.")
