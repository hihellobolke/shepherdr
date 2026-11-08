"""The digest a child Task leaves when it ends (proposal 6.14, contract C5).

The digest is built on the child's own machine, written into `feature.digests` (keyed by
`child_key`), and carried to the home machine by `feature-status` (C8). It holds: the review
findings by level, the needs-you/stalled/failed events and the most expensive tool calls read
from the agent's session log by `core/chat-log.js`, the transcript path, the rebase conflicts,
the review rounds used and the time against the estimate.

There is no CLI entry: `tick.py` calls `build` when a child ends.
"""

import json
import os
import re
import subprocess
import tempfile

from .base import APP_HOME, hc_json, now, PACKAGE
from .agents import agent_log

# The whole field list, in order, for the tests (C5).
DIGEST_FIELDS = [
    "ref", "role", "feature", "rows", "state", "machine", "review", "rounds",
    "events", "rebase_conflicts", "time", "top_tools", "transcript",
]

# A recorded review round's `result` value that counts as a round (commands.REVIEWED).
REVIEWED = ("report", "pass", "changes")

# The three finding levels the reviewer uses (templates/reviewer.md), in digest order.
LEVELS = ("blocker", "major", "minor")

# A tool_use that is a question to the person, not a tool call: counted as a needs-you event,
# never as a tool in `top_tools`.
NOT_A_TOOL = {"AskUserQuestion"}


def child_key(child):
    """The machine-qualified key for `feature.digests` (C2): `"<machine>:<id>"`, with `"local"`
    for the home machine. A child id is unique only within its own machine's store, so the key
    is qualified by machine to keep two machines' children apart. A Feature's `children` entry
    (C2) carries the numeric id in its `ref` field; a Task record carries it in `id`."""
    cid = child.get("ref")
    if cid is None:
        cid = child.get("id")
    return f"{child.get('machine') or 'local'}:{cid}"


def build(s, task):
    """The digest (C5) of one child Task: its record plus its replayed session log."""
    replay = _replay(task)
    return {
        "ref": s.ref(task),
        "role": task.get("role"),
        "feature": task.get("feature"),
        "rows": list(task.get("rows") or []),
        "state": _state(task),
        "machine": task.get("machine") or _this_machine(),
        "review": _review_counts(task),
        "rounds": _rounds_used(task),
        "events": replay["events"],
        "rebase_conflicts": int(task.get("rebase_conflicts") or 0),
        "time": _time(task),
        "top_tools": replay["top_tools"],
        "transcript": replay["transcript"] or agent_log(task),
    }


# ---- the record's own fields ----------------------------------------------------------------

def _state(task):
    """How the child ended: `merged`, `skipped`, `cancelled` or `failed` (C5)."""
    if task.get("skipped"):
        return "skipped"
    if task.get("cancelled") or task.get("state") == "cancelled":
        return "cancelled"
    if task.get("merged") or task.get("state") == "done":
        return "merged"
    return "failed"


def _rounds_used(task):
    """The review rounds used (commands.rounds_used)."""
    return sum(1 for r in task.get("reviews") or [] if r.get("result") in REVIEWED)


def _review_counts(task):
    """The findings of the last recorded round by level (C5). A `pass` round has none. The round
    stores its findings as one free line (`reviewed --report`), so the count is a best-effort read
    of that line: a quantified mention ("2 minor") wins, else each tagged occurrence of the level
    word, skipping "no"/"zero"/"none"."""
    counts = {level: 0 for level in LEVELS}
    rounds = [r for r in task.get("reviews") or [] if r.get("result") in REVIEWED]
    if not rounds:
        return counts
    last = rounds[-1]
    if last.get("result") == "pass":
        return counts
    text = last.get("text") or ""
    for level in LEVELS:
        quant = re.findall(rf"(\d+)\s+{level}s?\b", text, re.I)
        if quant:
            counts[level] = sum(int(n) for n in quant)
            continue
        hits = 0
        for m in re.finditer(rf"\b{level}s?\b", text, re.I):
            before = text[max(0, m.start() - 6):m.start()].lower()
            if re.search(r"(no|zero|none)\s*$", before):
                continue
            hits += 1
        counts[level] = hits
    return counts


def _time(task):
    """Minutes against the estimate (C5). `estimate_min` is null when the Task gave no estimate."""
    started = task.get("started")
    estimate = task.get("estimate_min")
    estimate = estimate if isinstance(estimate, (int, float)) else None
    if started is None:
        return {"estimate_min": estimate, "actual_min": None}
    end = task.get("done_at") or task.get("ended_at") or now()
    actual = round((end - started) / 60000) if end >= started else None
    return {"estimate_min": estimate, "actual_min": actual}


def _this_machine():
    """This machine's herdr-core name, or None (used when the task has no `machine` of its own)."""
    try:
        d = hc_json("server", "status") or {}
        return (d.get("core") or {}).get("name") or None
    except Exception:
        return None


# ---- the replayed session log (through core/chat-log.js) -------------------------------------

def _last_agent(task):
    """The last agent of the Task that kept a session log, or None."""
    agents = [a for a in task.get("agents") or [] if a.get("log")]
    return agents[-1] if agents else None


def _chat_log_js():
    """Where `core/chat-log.js` lives at run time: the installed app, else the repo beside this
    package, else the chat mod's copy. None when it cannot be found."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(PACKAGE))))
    for p in (
        os.path.join(APP_HOME, "core", "chat-log.js"),
        os.path.join(repo, "core", "chat-log.js"),
        os.path.join(APP_HOME, "mods", "chat", "core", "chat-log.js"),
        os.path.join(repo, "mods", "chat", "core", "chat-log.js"),
    ):
        if os.path.isfile(p):
            return p
    return None


# The Node side: replay a log through core/chat-log.js (the events) and read the tool calls and
# the transcript path. Events come from the Conversation core/chat-log.js builds (its `ask` items
# are needs-you, its `error` items are failed); stalls and the tool timings are read from the raw
# lines, which the Conversation does not time. argv: <log file> <kind> <chat-log.js path>.
_REPLAY_JS = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const [logFile, kind, chatLog] = process.argv.slice(2);
const out = { events: { needs_you: 0, stalled: 0, failed: 0 }, top_tools: [], transcript: null };
try {
  out.transcript = fs.realpathSync(logFile);
  const lines = fs.readFileSync(logFile, "utf8").split("\n");
  const mod = await import(pathToFileURL(chatLog).href);
  const conv = new mod.Conversation(kind === "codex" ? "codex" : "claude");
  let off = 0; const recs = [];
  for (const line of lines) { if (line) recs.push({ off, kind: "J", data: line }); off += line.length + 1; }
  conv.feed(recs);
  out.events.needs_you = conv.items.filter((i) => i.kind === "ask").length;
  out.events.failed = conv.items.filter((i) => i.kind === "error").length;
  const starts = new Map(), tally = new Map();
  const ts = (s) => Date.parse(s || "") || 0;
  const bump = (name, ms) => { const t = tally.get(name) || { tool: name, calls: 0, ms: 0 }; t.calls++; t.ms += Math.max(0, ms || 0); tally.set(name, t); };
  const NOT_A_TOOL = new Set(["AskUserQuestion", "request_user_input"]);
  for (const line of lines) {
    if (!line) continue;
    let l; try { l = JSON.parse(line); } catch { continue; }
    const t = ts(l.timestamp);
    if (l.type === "assistant" && Array.isArray(l.message?.content)) {
      for (const b of l.message.content) if (b?.type === "tool_use" && !NOT_A_TOOL.has(b.name)) starts.set(b.id, { name: b.name, t });
    } else if (l.type === "user" && l.message) {
      const c = l.message.content;
      if (Array.isArray(c)) {
        for (const b of c) {
          if (b?.type !== "tool_result") continue;
          const s = starts.get(b.tool_use_id);
          if (s) { bump(s.name, t - s.t); starts.delete(b.tool_use_id); }
        }
        if (c.some((b) => /\[Request interrupted/.test(typeof b?.content === "string" ? b.content : b?.text || ""))) out.events.stalled++;
      } else if (typeof c === "string" && /\[Request interrupted/.test(c)) out.events.stalled++;
    } else if (l.type === "response_item" && (l.payload?.type === "function_call" || l.payload?.type === "custom_tool_call" || l.payload?.type === "local_shell_call") && !NOT_A_TOOL.has(l.payload?.name)) {
      starts.set(String(l.payload.call_id ?? l.payload.id), { name: String(l.payload.name || "shell"), t });
    } else if (l.type === "response_item" && (l.payload?.type === "function_call_output" || l.payload?.type === "custom_tool_call_output")) {
      const s = starts.get(String(l.payload.call_id ?? l.payload.id));
      if (s) { bump(s.name, t - s.t); starts.delete(String(l.payload.call_id ?? l.payload.id)); }
    } else if ((l.type === "event_msg" || l.type === "response_item") && l.payload?.type === "turn_aborted") out.events.stalled++;
  }
  for (const s of starts.values()) bump(s.name, 0);
  out.top_tools = [...tally.values()].sort((a, b) => b.ms - a.ms).slice(0, 5);
} catch (e) { out.error = String((e && e.message) || e); }
process.stdout.write(JSON.stringify(out));
"""

_EMPTY = {"events": {"needs_you": 0, "stalled": 0, "failed": 0}, "top_tools": [], "transcript": None}


def _replay(task):
    """Replay the Task's session log through core/chat-log.js: the events, the tool calls and the
    transcript path. Degrades to the empty shape when there is no log, no Node or no chat-log.js,
    so a digest is always valid."""
    agent = _last_agent(task)
    if not agent or not os.path.isfile(agent["log"]):
        return dict(_EMPTY)
    chat_log = _chat_log_js()
    if not chat_log:
        return {**_EMPTY, "transcript": agent["log"]}
    kind = "codex" if "codex" in str(agent.get("agent") or "").lower() else "claude"
    script = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(_REPLAY_JS)
            script = f.name
        r = subprocess.run(["node", script, agent["log"], kind, chat_log],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**_EMPTY, "transcript": agent["log"]}
    finally:
        if script:
            try:
                os.unlink(script)
            except OSError:
                pass
    return {
        "events": {k: int((data.get("events") or {}).get(k) or 0) for k in ("needs_you", "stalled", "failed")},
        "top_tools": [{"tool": t.get("tool"), "calls": int(t.get("calls") or 0), "ms": int(t.get("ms") or 0)}
                      for t in (data.get("top_tools") or []) if t.get("tool")],
        "transcript": data.get("transcript") or agent["log"],
    }
