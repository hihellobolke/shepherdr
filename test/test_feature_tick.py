"""The Feature tick (tick.py, proposal T19, contract C8).

Each tick drives every live Feature in a folder: it brings the children's states and digests to the
home machine, stores a digest when a child ends, fires the fan-out trigger once the spec merge lands,
restarts a failed or stalled child once, moves the phase through `phases.phase_of`, counts the fix
rounds and raises Needs you once they run out, runs the closing steps, starts a retro for a done
Feature when the setting is automatic, and tells the Assistant the wave and phase-change summaries
plus the one-by-one exception events.

These tests prove each phase change, the one retry (and the surfaced failure after it), the Needs you
after the allowed fix rounds, the news shape, the digest write, the retro start, and that a classic
folder (no Features) is never touched.

A throwaway STORE and settings file (default `verify_rounds` 2) are used, and a plain throwaway
folder (no git is needed: the collaborators that shell out to git are mocked). Every cross-module
collaborator `tick.py` calls is mocked and driven through the contract signatures (C7): the Feature
tick is unit-tested, not the modules it orchestrates. The real `herdr_task.phases` (its pure rules
and its one list of phase names, C7) is used."""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
sys.dont_write_bytecode = True

from herdr_task import tick  # noqa: E402
from herdr_task import phases  # noqa: E402
from herdr_task import store as store_mod  # noqa: E402
from herdr_task import settings as settings_mod  # noqa: E402
from herdr_task import base as base_mod  # noqa: E402
from herdr_task.store import Store, locate, read_list  # noqa: E402

_PATCHES = []


def setUpModule():
    global _PATCHES
    store_dir = tempfile.mkdtemp(prefix="herdr-feature-tick-store-")
    settings_file = os.path.join(tempfile.mkdtemp(prefix="herdr-feature-tick-settings-"), "shepherdr.json")
    _PATCHES = [
        mock.patch.dict(os.environ, {
            "HERDR_TASK_STORE": store_dir,
            "HERDR_TASK_SETTINGS": settings_file,
        }),
        mock.patch.object(settings_mod, "SETTINGS", settings_file),
        mock.patch.object(store_mod, "STORE", store_dir),
        mock.patch.object(base_mod, "STORE", store_dir),
    ]
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()
    _PATCHES.clear()


def news_state_for(mapping):
    """A fake `news.news_state` driven by each Task's `_ev` test marker: `failed`, `stalled`,
    `needs` (a person-blocking wait, whose kind the status line decides), or none (working)."""
    def fake(s, t, running):
        ev = t.get("_ev")
        if ev == "failed":
            return ({"status": "needs", "needs": "stopped", "why": "Agent stopped"}, "failed", None)
        if ev == "stalled":
            return ({"status": "working", "needs": None, "why": None}, None, ("quiet:1", "went quiet"))
        if ev == "needs":
            return ({"status": "needs", "needs": "reply", "why": "waiting"}, "needs-you", None)
        return ({"status": "working", "needs": None, "why": None}, None, None)
    return fake


class FeatureTickBase(unittest.TestCase):
    """A throwaway folder and store, with every collaborator `tick.py` reaches mocked."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="herdr-feature-tick-")
        # The collaborators that shell out, reach other machines, or send to the Assistant: mocked so
        # the Feature pass is tested on its own. news_state is driven per Task by its `_ev` marker.
        self.m = {
            "gather": mock.patch.object(tick.feature_status, "gather", return_value={"children": []}),
            "spec_merged": mock.patch.object(tick.feature_mod, "spec_merged", return_value=False),
            "fanout": mock.patch.object(tick.fanout, "run",
                                        return_value={"created": {"children": 0, "verify": 0}}),
            "close": mock.patch.object(tick.closing, "close", return_value={}),
            "retro": mock.patch.object(tick.retro, "start", return_value=None),
            "coordinator": mock.patch.object(tick, "coordinator_in", return_value=None),
            "send": mock.patch.object(tick.news, "send_or_hold", return_value="sent"),
            "live": mock.patch.object(tick, "live_agents", return_value=[]),
            "run": mock.patch.object(tick, "run", return_value=mock.Mock(returncode=0)),
            "news_state": mock.patch.object(tick.news, "news_state", side_effect=news_state_for({})),
        }
        self.mocks = {k: p.start() for k, p in self.m.items()}
        self.addCleanup(lambda: [p.stop() for p in self.m.values()])

    def store(self):
        s = Store(locate(self.folder)).__enter__()
        s.data["prefix"] = "HAT"
        self.addCleanup(lambda: s.__exit__(None, None, None))
        return s

    def feature(self, s, phase="building", **fields):
        f = s.new_feature("Login flow", "add-login-flow", branch="feature/HAT-1-login", base="main")
        f["phase"] = phase
        f.update(fields)
        return f

    def child(self, s, feature, role="child", rows=("1.1",), **fields):
        t = s.new("child", "", "worktree", feature=feature["id"], role=role,
                  owns=["a.txt"], risk="low", rows=list(rows), retries=0, skipped=False)
        t.update(fields)
        feature.setdefault("children", []).append(
            {"ref": t["id"], "machine": None, "package": rows[0], "rows": list(rows), "role": role})
        return t

    def verify(self, s, feature, **fields):
        t = s.new("verify", "", "worktree", feature=feature["id"], role="verify", owns=[])
        t.update(fields)
        return t


class PhaseChanges(FeatureTickBase):

    def test_fanout_trigger_on_building_with_spec_merged(self):
        s = self.store()
        f = self.feature(s, phase="building")
        f["spec_task"] = s.new("Plan", "", "worktree", feature=f["id"], role="spec", owns=[])["id"]
        self.mocks["spec_merged"].return_value = True
        self.mocks["fanout"].return_value = {"created": {"children": 3, "verify": 1}}
        report = {}
        tick.one_feature(s, f, report)
        self.mocks["fanout"].assert_called_once()
        self.assertEqual(self.mocks["fanout"].call_args.args[1], "add-login-flow")
        self.assertTrue(any(n["event"] == "fanout" for n in report.get("feature_news", [])))

    def test_fanout_not_fired_when_children_exist(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="running")
        self.mocks["spec_merged"].return_value = True
        tick.one_feature(s, f, {})
        self.mocks["fanout"].assert_not_called()

    def test_building_to_verifying_when_all_children_satisfied(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="done", merged=True)
        self.child(s, f, rows=("1.2",), state="done", merged=True)
        self.verify(s, f)  # the first Verify Task, not reported yet
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(f["phase"], "verifying")
        self.assertTrue(any(n["event"] == "phase" and "Verifying" in n["text"]
                            for n in report.get("feature_news", [])))

    def test_skipped_child_counts_as_satisfied(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="done", merged=True)
        self.child(s, f, rows=("1.2",), state="cancelled", skipped=True)
        self.verify(s, f)
        tick.one_feature(s, f, {})
        self.assertEqual(f["phase"], "verifying")

    def test_verify_pass_moves_to_closing(self):
        s = self.store()
        f = self.feature(s, phase="verifying")
        self.child(s, f, rows=("1.1",), state="done", merged=True)
        v = self.verify(s, f, verify_pass=True)
        f["verify_task"] = v["id"]
        tick.one_feature(s, f, {})
        self.assertEqual(f["phase"], "closing")
        self.mocks["close"].assert_called_once()

    def test_closing_moves_to_ready_via_closing_close(self):
        s = self.store()
        f = self.feature(s, phase="closing")
        self.child(s, f, rows=("1.1",), state="done", merged=True)

        def close(store, feat):
            feat["phase"] = "ready"
            feat["attention"] = ""
            return {"pr": "https://example/pr/1"}
        self.mocks["close"].side_effect = close
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(f["phase"], "ready")
        self.assertTrue(any(n["event"] == "phase" and "Ready" in n["text"]
                            for n in report.get("feature_news", [])))

    def test_closing_row_no_child_attention_is_kept(self):
        s = self.store()
        f = self.feature(s, phase="closing")
        self.child(s, f, rows=("1.1",), state="done", merged=True)

        def close(store, feat):
            feat["attention"] = "row-no-child"  # phase stays closing
            return {}
        self.mocks["close"].side_effect = close
        tick.one_feature(s, f, {})
        self.assertEqual(f["phase"], "closing")
        self.assertEqual(f["attention"], "row-no-child")


class Retry(FeatureTickBase):

    def test_failed_child_is_restarted_once(self):
        s = self.store()
        f = self.feature(s, phase="building")
        c = self.child(s, f, rows=("1.1",), state="running", _ev="failed")
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(c["retries"], 1)
        self.assertEqual(c["state"], "queued")
        self.assertNotIn("attention", c)
        self.assertEqual(f["attention"], "")  # not surfaced on the first failure
        self.assertTrue(any(r["event"] == "failed" for r in report.get("feature_retry", [])))

    def test_stalled_child_is_restarted_once(self):
        s = self.store()
        f = self.feature(s, phase="building")
        c = self.child(s, f, rows=("1.1",), state="running", _ev="stalled")
        tick.one_feature(s, f, {})
        self.assertEqual(c["retries"], 1)
        self.assertEqual(c["state"], "queued")

    def test_second_failure_surfaces_child_failed(self):
        s = self.store()
        f = self.feature(s, phase="building")
        # Its one retry is already used; a second failure surfaces on the Feature.
        c = self.child(s, f, rows=("1.1",), state="running", retries=1, _ev="failed")
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(c["attention"], "failed")
        self.assertEqual(c["retries"], 1)  # not restarted again
        self.assertEqual(f["attention"], phases.ATTENTION_CHILD_FAILED)
        self.assertTrue(any(n["event"] == "failed" for n in report.get("feature_news", [])))


class FixRounds(FeatureTickBase):

    def _verifying_with_findings(self, s, n_verifies):
        """A verifying Feature whose current Verify Task reports findings, with `n_verifies` Verify
        Tasks recorded (one per round plus the first)."""
        f = self.feature(s, phase="verifying")
        self.child(s, f, rows=("1.1",), state="done", merged=True)
        verifies = [self.verify(s, f) for _ in range(n_verifies)]
        for v in verifies[:-1]:
            v["verify_pass"] = False
            v["findings"] = [{"level": "major", "title": "x"}]
        current = verifies[-1]
        current["verify_pass"] = False
        current["findings"] = [{"level": "major", "title": "still open", "paths": [], "row": None}]
        f["verify_task"] = current["id"]
        return f

    def test_within_rounds_no_needs_you(self):
        s = self.store()
        f = self._verifying_with_findings(s, n_verifies=2)  # one fix round used, 2 allowed
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(f["phase"], "verifying")
        self.assertEqual(f["fix_rounds"], 1)
        self.assertEqual(f["attention"], "")
        self.assertTrue(any(n["event"] == "verify-findings" for n in report.get("feature_news", [])))

    def test_needs_you_after_two_fix_rounds(self):
        s = self.store()
        f = self._verifying_with_findings(s, n_verifies=3)  # two fix rounds used, 2 allowed
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(f["phase"], "verifying")
        self.assertEqual(f["fix_rounds"], 2)
        self.assertEqual(f["attention"], phases.ATTENTION_VERIFY_OPEN)

    def test_fix_in_flight_holds_the_round(self):
        s = self.store()
        f = self._verifying_with_findings(s, n_verifies=3)
        # A Fix Task of the round is still running: no Needs you yet, whatever the findings say.
        self.child(s, f, role="fix", rows=("1.1",), state="running")
        tick.one_feature(s, f, {})
        self.assertEqual(f["attention"], "")


class ChildEvents(FeatureTickBase):

    def test_needs_owns_from_status_line(self):
        s = self.store()
        f = self.feature(s, phase="building")
        c = self.child(s, f, rows=("1.1",), state="running", retries=1, _ev="needs",
                       progress={"text": "needs owns change for src/b.ts"})
        report = {}
        tick.one_feature(s, f, report)
        self.assertEqual(c["attention"], "needs-owns")
        self.assertEqual(f["attention"], phases.ATTENTION_NEEDS_OWNS)
        self.assertTrue(any(n["event"] == "needs-owns" for n in report.get("feature_news", [])))

    def test_needs_plan_from_status_line(self):
        s = self.store()
        f = self.feature(s, phase="building")
        c = self.child(s, f, rows=("1.1",), state="running", retries=1, _ev="needs",
                       progress={"text": "needs plan change: split the parser row"})
        tick.one_feature(s, f, {})
        self.assertEqual(c["attention"], "needs-plan")
        self.assertEqual(f["attention"], phases.ATTENTION_NEEDS_PLAN)

    def test_risky_task_waiting_is_announced(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="ready", risk="high")
        report = {}
        tick.one_feature(s, f, report)
        self.assertTrue(any(n["event"] == "risky-waiting" for n in report.get("feature_news", [])))

    def test_exception_news_is_sent_once(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="running", retries=1, _ev="failed")
        r1 = {}
        tick.one_feature(s, f, r1)
        r2 = {}
        tick.one_feature(s, f, r2)
        self.assertTrue(any(n["event"] == "failed" for n in r1.get("feature_news", [])))
        self.assertFalse(any(n["event"] == "failed" for n in r2.get("feature_news", [])))


class News(FeatureTickBase):

    def test_news_line_shape(self):
        s = self.store()
        f = self.feature(s, phase="building")
        line = tick._feature_news_line("HAT-1", "phase", "HAT-1 moved to Building: Login flow")
        self.assertRegex(line, r'^<herdr-news feature="HAT-1" event="phase">.*</herdr-news>$')

    def test_news_line_neutralises_angle_brackets_in_the_body(self):
        # Mirrors news.news_line: angle brackets in the text become single-guillemets so the body
        # never closes the element; the attributes are entity-escaped.
        line = tick._feature_news_line('a "ref"', "failed", "a <script> tag")
        self.assertNotIn("<script>", line)
        self.assertIn("‹script›", line)
        self.assertIn("feature=\"a &quot;ref&quot;\"", line)

    def test_wave_end_summary(self):
        s = self.store()
        f = self.feature(s, phase="building")
        self.child(s, f, rows=("1.1",), state="done", merged=True)
        self.child(s, f, rows=("1.2",), state="done", merged=True)
        self.verify(s, f)
        report = {}
        tick.one_feature(s, f, report)
        self.assertTrue(any(n["event"] == "wave" for n in report.get("feature_news", [])))
        self.assertIn("1", f["waves_told"])
        # Told once: a second tick does not repeat the wave summary.
        report2 = {}
        tick.one_feature(s, f, report2)
        self.assertFalse(any(n["event"] == "wave" for n in report2.get("feature_news", [])))


class Digests(FeatureTickBase):

    def test_digest_from_gather_is_stored(self):
        s = self.store()
        f = self.feature(s, phase="building")
        c = self.child(s, f, rows=("1.1",), state="done", merged=True)
        d = {"ref": "HAT-%d" % c["id"], "state": "merged", "rows": ["1.1"]}
        self.mocks["gather"].return_value = {
            "children": [{"ref": "HAT-%d" % c["id"], "state": "done", "role": "child",
                          "rows": ["1.1"], "digest": d}],
        }
        tick.one_feature(s, f, {})
        key = "local:%d" % c["id"]
        self.assertEqual(f["digests"].get(key), d)


class Retro(FeatureTickBase):

    def test_retro_starts_automatically_on_done(self):
        s = self.store()
        f = self.feature(s, phase="done")
        with mock.patch.object(settings_mod, "settings_for",
                               return_value={"tasks": {"retro": "automatic", "verify_rounds": 2}}):
            self.mocks["retro"].return_value = {"id": 99}
            report = {}
            tick.one_feature(s, f, report)
        self.mocks["retro"].assert_called_once()
        self.assertTrue(f.get("retro_handled"))
        self.assertIn("HAT-%d" % f["id"], report.get("feature_retro", []))

    def test_retro_not_started_when_off(self):
        s = self.store()
        f = self.feature(s, phase="done")
        with mock.patch.object(settings_mod, "settings_for",
                               return_value={"tasks": {"retro": "off", "verify_rounds": 2}}):
            tick.one_feature(s, f, {})
        self.mocks["retro"].assert_not_called()
        self.assertTrue(f.get("retro_handled"))  # handled once, not scanned again

    def test_done_feature_handled_once(self):
        s = self.store()
        f = self.feature(s, phase="done", retro_handled=True)
        tick.one_feature(s, f, {})
        self.mocks["retro"].assert_not_called()


class Classic(FeatureTickBase):

    def test_classic_folder_is_untouched(self):
        s = self.store()
        t = s.new("Fix the bug", "do it", "worktree")  # a classic Task, no feature fields
        t["state"] = "running"
        import copy
        before = copy.deepcopy(t)
        report = {}
        tick.features_tick(s, report)
        self.assertEqual(t, before)
        self.assertNotIn("feature_news", report)
        self.assertNotIn("feature_error", report)

    def test_live_feature_detection(self):
        self.assertFalse(tick._live_feature({"tasks": []}))
        self.assertFalse(tick._live_feature({"features": [{"phase": "cancelled"}]}))
        self.assertFalse(tick._live_feature({"features": [{"phase": "done", "retro_handled": True}]}))
        self.assertTrue(tick._live_feature({"features": [{"phase": "building"}]}))
        self.assertTrue(tick._live_feature({"features": [{"phase": "done"}]}))  # retro not handled yet

    def test_features_tick_isolates_one_feature_error(self):
        s = self.store()
        good = self.feature(s, phase="building")
        self.child(s, good, rows=("1.1",), state="done", merged=True)
        self.verify(s, good)
        bad = self.feature(s, phase="building")
        bad["id"] = good["id"]  # a broken record is still isolated
        with mock.patch.object(tick, "one_feature", side_effect=[RuntimeError("boom"), None]):
            report = {}
            tick.features_tick(s, report)
        self.assertIn("feature_error", report)


if __name__ == "__main__":
    unittest.main()
