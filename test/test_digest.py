"""digest.py: the child digest (contract C5). A real log is replayed through core/chat-log.js
for the events, the tool calls and the transcript path. Mocks nothing but the machine name; the
log and Store are real throwaway files (AGENTS.md)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.dont_write_bytecode = True
if BIN not in sys.path:
    sys.path.insert(0, BIN)


def _claude_log(path):
    """A small but real-shaped Claude Code session log: two timed tool calls, a question to the
    person (AskUserQuestion) and an API error."""
    lines = [
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-08T00:00:00.000Z",
         "message": {"id": "m1", "model": "claude", "content": [{"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "uuid": "r1", "timestamp": "2026-11-08T00:00:03.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "files"}]}},
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-11-08T00:00:05.000Z",
         "message": {"id": "m2", "model": "claude", "content": [{"type": "tool_use", "id": "u2", "name": "Read", "input": {"file_path": "/x"}}]}},
        {"type": "user", "uuid": "r2", "timestamp": "2026-11-08T00:00:06.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "u2", "content": "data"}]}},
        {"type": "assistant", "uuid": "a3", "timestamp": "2026-11-08T00:00:08.000Z",
         "message": {"id": "m3", "model": "claude", "content": [{"type": "tool_use", "id": "u3", "name": "AskUserQuestion", "input": {"questions": [{"question": "Which one?"}]}}]}},
        {"type": "assistant", "uuid": "a4", "timestamp": "2026-11-08T00:00:09.000Z", "isApiErrorMessage": True,
         "message": {"content": [{"type": "text", "text": "API Error: overloaded"}]}},
    ]
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class DigestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["HERDR_TASK_STORE"] = os.path.join(self.tmp, "store")
        from herdr_task import store, digest
        self.store, self.digest = store, digest
        # Never reach hc for the machine name in a test.
        self._orig_machine = digest._this_machine
        digest._this_machine = lambda: "home-mac"

    def tearDown(self):
        self.digest._this_machine = self._orig_machine
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("HERDR_TASK_STORE", None)

    def _store(self):
        return self.store.Store(self.store.locate(self.tmp))

    def _task(self, s, **fields):
        t = s.new(fields.pop("title", "Child"))
        t.update(fields)
        return t


class ChildKey(DigestBase):
    def test_local_and_remote(self):
        self.assertEqual(self.digest.child_key({"id": 14, "machine": None}), "local:14")
        self.assertEqual(self.digest.child_key({"id": 15, "machine": "vm-b"}), "vm-b:15")
        self.assertEqual(self.digest.child_key({"id": 16}), "local:16")


class State(DigestBase):
    def test_states(self):
        st = self.digest._state
        self.assertEqual(st({"state": "done", "merged": True}), "merged")
        self.assertEqual(st({"state": "done"}), "merged")
        self.assertEqual(st({"skipped": True, "state": "done"}), "skipped")
        self.assertEqual(st({"cancelled": True}), "cancelled")
        self.assertEqual(st({"state": "cancelled"}), "cancelled")
        self.assertEqual(st({"state": "running"}), "failed")


class ReviewCounts(DigestBase):
    def test_no_rounds_is_zero(self):
        self.assertEqual(self.digest._review_counts({}), {"blocker": 0, "major": 0, "minor": 0})

    def test_pass_round_is_zero(self):
        t = {"reviews": [{"result": "pass", "text": "all good"}]}
        self.assertEqual(self.digest._review_counts(t), {"blocker": 0, "major": 0, "minor": 0})

    def test_counts_tagged_findings_of_the_last_round(self):
        t = {"reviews": [
            {"result": "report", "text": "blocker: boom"},
            {"result": "changes", "text": "major: missing guard; minor: rename x; minor: dead code"},
        ]}
        self.assertEqual(self.digest._review_counts(t), {"blocker": 0, "major": 1, "minor": 2})

    def test_quantified_mentions(self):
        t = {"reviews": [{"result": "report", "text": "1 blocker, 2 minor issues, no major ones"}]}
        self.assertEqual(self.digest._review_counts(t), {"blocker": 1, "major": 0, "minor": 2})

    def test_rounds_used_counts_recorded_results_only(self):
        t = {"reviews": [{"result": "report"}, {"result": "note"}, {"result": "pass"}]}
        self.assertEqual(self.digest._rounds_used(t), 2)


class Time(DigestBase):
    def test_estimate_null_when_none(self):
        t = self.digest._time({"started": 1000, "done_at": 1000 + 42 * 60000})
        self.assertEqual(t, {"estimate_min": None, "actual_min": 42})

    def test_estimate_and_ended_at(self):
        t = self.digest._time({"started": 0, "ended_at": 30 * 60000, "estimate_min": 20})
        self.assertEqual(t, {"estimate_min": 20, "actual_min": 30})

    def test_no_start_gives_null_actual(self):
        self.assertEqual(self.digest._time({}), {"estimate_min": None, "actual_min": None})


class BuildNoLog(DigestBase):
    def test_valid_shape_without_a_log(self):
        with self._store() as s:
            t = self._task(s, feature=12, role="child", rows=["2.2"], machine="vm-b",
                           state="done", merged=True)
            d = self.digest.build(s, t)
        self.assertEqual(list(d.keys()), self.digest.DIGEST_FIELDS)
        self.assertEqual(d["events"], {"needs_you": 0, "stalled": 0, "failed": 0})
        self.assertEqual(d["top_tools"], [])
        self.assertIsNone(d["transcript"])
        self.assertEqual(d["machine"], "vm-b")

    def test_machine_falls_back_to_this_machine(self):
        with self._store() as s:
            t = self._task(s, feature=12, role="child")
            d = self.digest.build(s, t)
        self.assertEqual(d["machine"], "home-mac")


@unittest.skipUnless(shutil.which("node"), "needs Node to replay through core/chat-log.js")
class BuildWithLog(DigestBase):
    def test_real_log_gives_events_tools_and_transcript(self):
        log = os.path.join(self.tmp, "session.jsonl")
        _claude_log(log)
        with self._store() as s:
            t = self._task(s, feature=12, role="child", rows=["2.2"], machine="vm-b",
                           state="done", merged=True, started=1000, done_at=1000 + 42 * 60000,
                           rebase_conflicts=1, estimate_min=30,
                           reviews=[{"result": "report", "text": "major: X; minor: Y; minor: Z"}],
                           agents=[{"log": log, "agent": "claude"}])
            d = self.digest.build(s, t)
        self.assertEqual(list(d.keys()), self.digest.DIGEST_FIELDS)
        # Events: the question is a needs-you, the API error is a failed (through core/chat-log.js).
        self.assertEqual(d["events"], {"needs_you": 1, "stalled": 0, "failed": 1})
        # The transcript is the real log path.
        self.assertEqual(d["transcript"], os.path.realpath(log))
        # The tool calls, longest first, with AskUserQuestion left out (it is the question, C5).
        tools = {t["tool"]: t for t in d["top_tools"]}
        self.assertIn("Bash", tools)
        self.assertIn("Read", tools)
        self.assertNotIn("AskUserQuestion", tools)
        self.assertEqual(d["top_tools"][0]["tool"], "Bash")
        self.assertEqual(tools["Bash"]["ms"], 3000)
        self.assertEqual(tools["Bash"]["calls"], 1)
        # The record's own fields.
        self.assertEqual(d["review"], {"blocker": 0, "major": 1, "minor": 2})
        self.assertEqual(d["rounds"], 1)
        self.assertEqual(d["rebase_conflicts"], 1)
        self.assertEqual(d["time"], {"estimate_min": 30, "actual_min": 42})
        self.assertEqual(d["state"], "merged")


if __name__ == "__main__":
    unittest.main()
