"""Phase rules (T11a): the pure rules that decide a Feature's phase and attention from its children's
states, whether a fix round is allowed, whether a failed child takes its one retry, and which
dependents a failure or a skip blocks or unblocks (proposal sections 6.3, 6.11 and 6.12).

Everything here is pure: the tests build plain dicts, with no git, no store and no network.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

from herdr_task import phases


def child(cid, role="child", **kw):
    """A flow child Task record. Defaults to a plain running child; kw overrides any field."""
    t = {"id": cid, "ref": "HAT-%d" % cid, "role": role, "state": "running"}
    t.update(kw)
    return t


def merged(cid, **kw):
    return child(cid, state="done", merged=True, **kw)


def feature(phase, **kw):
    f = {"id": 12, "phase": phase, "attention": "", "fix_rounds": 0, "verify_task": None}
    f.update(kw)
    return f


class Constants(unittest.TestCase):
    """The phase and attention names and the event sets match the contract (C2, C8)."""

    def test_the_seven_phases(self):
        self.assertEqual(
            phases.PHASES,
            ("planning", "building", "verifying", "closing", "ready", "done", "cancelled"),
        )

    def test_the_attention_codes(self):
        self.assertEqual(
            set(phases.ATTENTION),
            {"verify-open", "row-no-child", "child-failed", "child-stalled", "needs-plan",
             "needs-owns", "base-conflict", "blocker-finding"},
        )

    def test_routine_events(self):
        self.assertEqual(phases.ROUTINE_EVENTS, ("started", "ready", "merged"))

    def test_exception_events_cover_the_one_by_one_events(self):
        for ev in ("needs-you", "failed", "stalled", "needs-plan", "needs-owns",
                   "risky-waiting", "verify-findings"):
            self.assertIn(ev, phases.EXCEPTION_EVENTS)


class IsRoutine(unittest.TestCase):
    """is_routine: true only for the counted events, false for the one-by-one exceptions."""

    def test_routine_events_are_routine(self):
        for ev in ("started", "ready", "merged"):
            self.assertTrue(phases.is_routine(ev))

    def test_exception_events_are_not_routine(self):
        for ev in ("needs-you", "failed", "stalled", "needs-plan", "needs-owns", "verify-findings"):
            self.assertFalse(phases.is_routine(ev))

    def test_an_unknown_event_is_not_routine(self):
        self.assertFalse(phases.is_routine("whatever"))


class Planning(unittest.TestCase):
    """planning stays planning: the move to building is the Plan approval hook, not a phase rule."""

    def test_planning_stays_planning_with_no_attention(self):
        self.assertEqual(phases.phase_of(feature("planning"), []), ("planning", ""))

    def test_a_default_record_with_no_phase_is_planning(self):
        self.assertEqual(phases.phase_of({"id": 1}, []), ("planning", ""))


class BuildingToVerifying(unittest.TestCase):
    """building to verifying: every child merged or skipped, and not before."""

    def test_children_still_running_stays_building(self):
        kids = [merged(14), child(15)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", ""))

    def test_no_children_yet_stays_building(self):
        self.assertEqual(phases.phase_of(feature("building"), []), ("building", ""))

    def test_all_children_merged_moves_to_verifying(self):
        kids = [merged(14), merged(15)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("verifying", ""))

    def test_a_skipped_child_counts_as_satisfied(self):
        kids = [merged(14), child(15, skipped=True, state="todo")]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("verifying", ""))

    def test_a_done_child_that_did_not_merge_does_not_satisfy(self):
        kids = [merged(14), child(15, state="done", merged=False)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", ""))

    def test_the_spec_and_verify_tasks_do_not_count_as_children(self):
        # Only role=="child" tasks gate the transition; a merged child alone moves it on.
        kids = [merged(14), child(13, role="spec", state="done", merged=True)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("verifying", ""))


class BuildingExceptions(unittest.TestCase):
    """building: a child's exception surfaces on the Feature, after the one retry is used."""

    def test_a_failed_child_with_a_retry_left_does_not_surface_yet(self):
        kids = [child(14, state="running", attention="failed", retries=0), child(15)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", ""))

    def test_a_failed_child_with_no_retry_left_shows_child_failed(self):
        kids = [child(14, attention="failed", retries=1), child(15)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "child-failed"))

    def test_a_stalled_child_with_no_retry_left_shows_child_stalled(self):
        kids = [child(14, attention="stalled", retries=1)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "child-stalled"))

    def test_needs_plan_surfaces_at_once(self):
        kids = [child(14, attention="needs-plan")]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "needs-plan"))

    def test_needs_owns_surfaces_at_once(self):
        kids = [child(14, attention="needs-owns")]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "needs-owns"))

    def test_child_failed_outranks_needs_plan_when_both_present(self):
        kids = [child(15, attention="needs-plan"), child(14, attention="failed", retries=1)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "child-failed"))

    def test_an_exception_blocks_the_move_to_verifying(self):
        # Even with another child merged, the failed child holds the Feature in building.
        kids = [merged(15), child(14, attention="failed", retries=1)]
        self.assertEqual(phases.phase_of(feature("building"), kids), ("building", "child-failed"))


class ShouldRetry(unittest.TestCase):
    """should_retry: a failed or stalled child restarts once and only once (section 6.12)."""

    def test_a_failed_child_with_no_retries_used_may_retry(self):
        self.assertTrue(phases.should_retry({"attention": "failed", "retries": 0}))

    def test_a_failed_child_with_no_retries_field_may_retry(self):
        self.assertTrue(phases.should_retry({"attention": "failed"}))

    def test_a_stalled_child_may_retry(self):
        self.assertTrue(phases.should_retry({"attention": "stalled", "retries": 0}))

    def test_a_child_that_used_its_retry_may_not_retry_again(self):
        self.assertFalse(phases.should_retry({"attention": "failed", "retries": 1}))

    def test_a_needs_plan_child_does_not_retry(self):
        self.assertFalse(phases.should_retry({"attention": "needs-plan", "retries": 0}))

    def test_a_healthy_child_does_not_retry(self):
        self.assertFalse(phases.should_retry({"attention": "", "retries": 0}))

    def test_a_skipped_child_does_not_retry_even_if_it_had_failed(self):
        # A retry must not undo a skip: the person resolved the row by skipping it.
        self.assertFalse(phases.should_retry({"attention": "failed", "retries": 0, "skipped": True}))

    def test_a_cancelled_child_does_not_retry(self):
        self.assertFalse(
            phases.should_retry({"attention": "failed", "retries": 0, "state": "cancelled"}))


class SkippedUnblocksDependents(unittest.TestCase):
    """A skip unblocks a chain: blocked_dependents shrinks, and the Feature moves on (section 6.12)."""

    def test_a_failure_blocks_only_the_children_that_depend_on_it(self):
        kids = [
            child(14, attention="failed", retries=1),  # failed, no retry left
            child(15, state="todo", after=[14]),       # waits on 14
            child(16, state="running"),                # an independent chain
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["HAT-15"])

    def test_the_block_propagates_down_the_chain(self):
        kids = [
            child(14, attention="failed", retries=1),
            child(15, state="todo", after=[14]),
            child(16, state="todo", after=[15]),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["HAT-15", "HAT-16"])

    def test_skipping_the_failed_child_unblocks_its_dependents(self):
        kids = [
            child(14, attention="failed", retries=1, skipped=True),
            child(15, state="todo", after=[14]),
        ]
        self.assertEqual(phases.blocked_dependents(kids), [])

    def test_skipping_an_intermediate_row_unblocks_everything_below_it(self):
        # 14 failed, 15 skipped (waits on 14), 16 waits on 15. The skip of 15 frees 16.
        kids = [
            child(14, attention="failed", retries=1),
            child(15, state="todo", after=[14], skipped=True),
            child(16, state="todo", after=[15]),
        ]
        self.assertEqual(phases.blocked_dependents(kids), [])

    def test_a_retryable_child_does_not_block(self):
        # Still has its retry: tick restarts it, so it does not block its dependents yet.
        kids = [child(14, attention="failed", retries=0), child(15, state="todo", after=[14])]
        self.assertEqual(phases.blocked_dependents(kids), [])

    def test_ids_do_not_collide_across_machines(self):
        # A:14 failed; B:15 waits on its own machine's 14, not on A:14. B:15 is not blocked.
        kids = [
            child(14, attention="failed", retries=1, machine="vm-a", ref="HAT-14"),
            child(15, state="todo", after=[14], machine="vm-b", ref="WEB-15"),
        ]
        self.assertEqual(phases.blocked_dependents(kids), [])

    def test_a_blocked_remote_dependent_is_named_machine_qualified(self):
        # A:14 failed; A:15 (same machine) waits on it and is blocked, reported as vm-a:HAT-15.
        kids = [
            child(14, attention="failed", retries=1, machine="vm-a", ref="HAT-14"),
            child(15, state="todo", after=[14], machine="vm-a", ref="HAT-15"),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["vm-a:HAT-15"])

    def test_same_ref_on_two_machines_is_told_apart(self):
        # Only vm-a:HAT-15 is blocked; vm-b:HAT-15 is independent and must not be swept in.
        kids = [
            child(14, attention="failed", retries=1, machine="vm-a", ref="HAT-14"),
            child(15, state="todo", after=[14], machine="vm-a", ref="HAT-15"),
            child(15, state="running", machine="vm-b", ref="HAT-15"),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["vm-a:HAT-15"])

    def test_a_cross_machine_link_between_waves_propagates_the_block(self):
        # vm-a:HAT-14 failed; vm-b:HAT-15 waits on it across machines (after_ext); vm-b:HAT-16 waits
        # on 15 locally. Both vm-b children are blocked (proposal 6.13 allows cross-machine links).
        kids = [
            child(14, attention="failed", retries=1, machine="vm-a", ref="HAT-14"),
            child(15, state="todo", machine="vm-b", ref="WEB-15",
                  after_ext=[{"ref": "vm-a:HAT-14", "machine": "vm-a", "prefix": "HAT", "id": 14}]),
            child(16, state="todo", after=[15], machine="vm-b", ref="WEB-16"),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["vm-b:WEB-15", "vm-b:WEB-16"])

    def test_a_cross_machine_link_back_to_the_home_machine_matches(self):
        # A home child (machine=None) failed; a remote child waits on it, naming home as "local".
        # The home identity is one key either way it is written, so the remote child is blocked.
        kids = [
            child(14, attention="failed", retries=1, machine=None, ref="HAT-14"),
            child(15, state="todo", machine="vm-b", ref="WEB-15",
                  after_ext=[{"ref": "HAT-14", "machine": "local", "prefix": "HAT", "id": 14}]),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["vm-b:WEB-15"])

    def test_a_home_child_written_local_is_named_without_a_prefix(self):
        # A home child spelled "local" is still home: it blocks locally and is named by its bare ref.
        kids = [
            child(14, attention="failed", retries=1, machine="local", ref="HAT-14"),
            child(15, state="todo", after=[14], machine="local", ref="HAT-15"),
        ]
        self.assertEqual(phases.blocked_dependents(kids), ["HAT-15"])

    def test_skip_lets_the_feature_reach_verifying(self):
        # 14 failed with no retry, 15 waits on it. Skip 14, 15 merges -> all satisfied -> verifying.
        f = feature("building")
        blocked = [child(14, attention="failed", retries=1), child(15, state="todo", after=[14])]
        self.assertEqual(phases.phase_of(f, blocked), ("building", "child-failed"))
        after_skip = [child(14, attention="failed", retries=1, skipped=True),
                      merged(15, after=[14])]
        self.assertEqual(phases.phase_of(f, after_skip), ("verifying", ""))


class Verifying(unittest.TestCase):
    """verifying: the Verify Task result drives closing, blocker-finding, or the fix-round loop."""

    def _verify(self, cid=30, **kw):
        kw.setdefault("state", "running")
        return child(cid, role="verify", **kw)

    def test_before_the_verify_task_reports_stays_verifying(self):
        f = feature("verifying", verify_task=30)
        kids = [merged(14), self._verify()]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", ""))

    def test_a_clean_verify_moves_to_closing(self):
        f = feature("verifying", verify_task=30)
        kids = [merged(14), self._verify(state="done", verify_pass=True)]
        self.assertEqual(phases.phase_of(f, kids), ("closing", ""))

    def test_open_findings_rest_quietly_until_the_caller_decides(self):
        # verify-open (needs the setting) and blocker-finding (needs the can-fix judgment) are the
        # caller's to raise; with none set yet, phase_of leaves the Feature quiet.
        f = feature("verifying", verify_task=30)
        findings = [{"level": "major", "title": "..."}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", ""))

    def test_phase_of_never_raises_blocker_finding_on_its_own(self):
        f = feature("verifying", verify_task=30)
        findings = [{"level": "blocker", "title": "Auth bypass"}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", ""))

    def test_a_verify_open_the_caller_set_is_kept(self):
        f = feature("verifying", verify_task=30, attention="verify-open")
        findings = [{"level": "major", "title": "..."}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", "verify-open"))

    def test_a_blocker_finding_the_caller_set_is_kept(self):
        f = feature("verifying", verify_task=30, attention="blocker-finding")
        findings = [{"level": "blocker", "title": "Auth bypass"}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", "blocker-finding"))

    def test_a_stale_unrelated_attention_is_cleared(self):
        # An attention that is not one of the two verifying ones does not survive the resting state.
        f = feature("verifying", verify_task=30, attention="child-failed")
        findings = [{"level": "major", "title": "..."}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", ""))

    def test_a_fix_round_in_flight_keeps_the_feature_quiet(self):
        # A fix round is running, so the Feature waits even if the caller had set verify-open before.
        f = feature("verifying", verify_task=30, attention="verify-open")
        findings = [{"level": "minor", "title": "..."}]
        kids = [merged(14), self._verify(verify_pass=False, findings=findings),
                child(31, role="fix", state="running")]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", ""))

    def test_a_named_verify_task_not_yet_present_keeps_waiting(self):
        # verify_task=32 names a fresh round's task that has not arrived; an old pass is not read.
        f = feature("verifying", verify_task=32)
        old = self._verify(30, state="done", verify_pass=True)
        self.assertEqual(phases.phase_of(f, [merged(14), old]), ("verifying", ""))

    def test_the_current_verify_task_is_the_one_named_by_the_record(self):
        # An old verify task reported findings; the new one (named by verify_task) is still running.
        f = feature("verifying", verify_task=32)
        old = self._verify(30, state="done", verify_pass=False, findings=[{"level": "major"}])
        new = self._verify(32, state="running")
        self.assertEqual(phases.phase_of(f, [merged(14), old, new]), ("verifying", ""))

    def test_a_fix_task_exception_surfaces_in_verifying(self):
        f = feature("verifying", verify_task=30)
        kids = [merged(14), self._verify(), child(31, role="fix", attention="failed", retries=1)]
        self.assertEqual(phases.phase_of(f, kids), ("verifying", "child-failed"))


class FixRounds(unittest.TestCase):
    """fix_round_allowed: a Feature allows verify_rounds rounds, two by default (section 6.11)."""

    def test_the_first_round_is_allowed(self):
        self.assertTrue(phases.fix_round_allowed(feature("verifying", fix_rounds=0), 2))

    def test_the_second_round_is_allowed(self):
        self.assertTrue(phases.fix_round_allowed(feature("verifying", fix_rounds=1), 2))

    def test_after_two_rounds_no_more_are_allowed(self):
        self.assertFalse(phases.fix_round_allowed(feature("verifying", fix_rounds=2), 2))

    def test_the_setting_can_raise_the_limit(self):
        self.assertTrue(phases.fix_round_allowed(feature("verifying", fix_rounds=2), 5))

    def test_two_rounds_then_needs_you(self):
        # The rule the caller pairs with verify-open: a round is allowed for fix_rounds 0 and 1, and
        # not for 2. The caller raises verify-open once this returns False (see the Verifying tests).
        for used, allowed in [(0, True), (1, True), (2, False)]:
            f = feature("verifying", verify_task=30, fix_rounds=used)
            self.assertEqual(phases.fix_round_allowed(f, 2), allowed)


class PreservedPhases(unittest.TestCase):
    """closing keeps the attention closing.close set; ready, done and cancelled are preserved."""

    def test_closing_keeps_its_attention(self):
        f = feature("closing", attention="row-no-child")
        self.assertEqual(phases.phase_of(f, [merged(14)]), ("closing", "row-no-child"))

    def test_closing_with_a_base_conflict_is_kept(self):
        f = feature("closing", attention="base-conflict")
        self.assertEqual(phases.phase_of(f, []), ("closing", "base-conflict"))

    def test_closing_with_no_attention(self):
        self.assertEqual(phases.phase_of(feature("closing"), [merged(14)]), ("closing", ""))

    def test_ready_is_preserved(self):
        self.assertEqual(phases.phase_of(feature("ready"), [merged(14)]), ("ready", ""))

    def test_done_is_preserved(self):
        self.assertEqual(phases.phase_of(feature("done"), [merged(14)]), ("done", ""))


class Cancel(unittest.TestCase):
    """cancelled is terminal: phase_of never leaves it, whatever the children look like."""

    def test_a_cancelled_feature_stays_cancelled(self):
        self.assertEqual(phases.phase_of(feature("cancelled"), []), ("cancelled", ""))

    def test_a_cancelled_feature_with_mergeable_children_still_stays_cancelled(self):
        kids = [merged(14), merged(15)]
        self.assertEqual(phases.phase_of(feature("cancelled"), kids), ("cancelled", ""))

    def test_a_cancelled_feature_drops_any_attention(self):
        self.assertEqual(phases.phase_of(feature("cancelled", attention="child-failed"), []),
                         ("cancelled", ""))


if __name__ == "__main__":
    unittest.main()
