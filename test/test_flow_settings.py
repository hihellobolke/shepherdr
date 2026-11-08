"""The OpenSpec flow settings on the Python side (docs/openspec-flow.md contract C1): the new
DEFAULTS, settings_for's layering for them, --set flow/verify.rounds/retro, the Settings block's
Flow and Retro lines, and the same facts in the Assistant's "Your settings" block. A space on the
classic flow with retro off keeps every output byte for byte as before: the Flow and Retro rows show
only when the setting is not its default."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

import herdr_task.settings as settings  # noqa: E402
import herdr_task.assistant_settings as aset  # noqa: E402
from herdr_task.base import Fail  # noqa: E402

FOLDER = "/f"


class Store:
    """Enough of herdr_task.store.Store for the settings helpers."""
    def __init__(self):
        self.loc = {"folder": FOLDER, "kind": "repo", "name": "f"}


def labels(lines):
    return [x["label"] for x in lines]


def line(block, label):
    return next((ln for ln in block.split("\n") if ln.startswith(f"- {label}: ")), None)


class Defaults(unittest.TestCase):
    def test_tasks_defaults(self):
        t = settings.DEFAULTS["tasks"]
        self.assertEqual(t["flow"], "classic")
        self.assertEqual(t["verify_rounds"], 2)
        self.assertEqual(t["risky_paths"], [])
        self.assertEqual(t["owns_exempt"], [])
        self.assertEqual(t["retro"], "off")
        self.assertEqual(t["retro_prompt"], "built-in")
        self.assertEqual(t["retro_save"], "outside")
        self.assertEqual(t["may_change"], [])

    def test_retro_is_a_role_section(self):
        self.assertEqual(settings.DEFAULTS["retro"], {})


class SettingsFor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.file = os.path.join(self.tmp, "shepherdr.json")
        p = mock.patch.object(settings, "SETTINGS", self.file)
        p.start()
        self.addCleanup(p.stop)
        self.s = Store()

    def write(self, d):
        with open(self.file, "w") as f:
            json.dump(d, f)

    def test_defaults_when_no_file(self):
        cfg = settings.settings_for(self.s)
        self.assertEqual(cfg["tasks"]["flow"], "classic")
        self.assertEqual(cfg["tasks"]["verify_rounds"], 2)
        self.assertEqual(cfg["tasks"]["retro"], "off")
        self.assertEqual(cfg["retro"], {})

    def test_everywhere_values_reach_a_folder_with_no_space_override(self):
        self.write({"everywhere": {"tasks": {"flow": "openspec", "verify_rounds": 4, "retro": "ask",
                                             "risky_paths": ["src/db/**"], "may_change": ["agents_md"]},
                                   "retro": {"agent": "claude", "model": "opus"}}})
        cfg = settings.settings_for(self.s, "sp1")
        self.assertEqual(cfg["tasks"]["flow"], "openspec")
        self.assertEqual(cfg["tasks"]["verify_rounds"], 4)
        self.assertEqual(cfg["tasks"]["retro"], "ask")
        self.assertEqual(cfg["tasks"]["risky_paths"], ["src/db/**"])
        self.assertEqual(cfg["tasks"]["may_change"], ["agents_md"])
        self.assertEqual(cfg["retro"], {"agent": "claude", "model": "opus"})

    def test_space_override_wins_the_rest_comes_from_everywhere(self):
        self.write({"everywhere": {"tasks": {"flow": "openspec", "verify_rounds": 4, "retro": "ask"}},
                    "spaces": {"sp1": {"folder": FOLDER, "tasks": {"retro": "automatic", "retro_save": "repo"}}}})
        cfg = settings.settings_for(self.s, "sp1")
        self.assertEqual(cfg["tasks"]["retro"], "automatic")
        self.assertEqual(cfg["tasks"]["retro_save"], "repo")
        self.assertEqual(cfg["tasks"]["flow"], "openspec")  # still Everywhere's
        self.assertEqual(cfg["tasks"]["verify_rounds"], 4)


class SetTask(unittest.TestCase):
    def test_flow_valid(self):
        t = {}
        settings.set_task(t, "flow", "openspec")
        self.assertEqual(t["set"]["flow"], "openspec")
        settings.set_task(t, "brief.flow", "classic")
        self.assertEqual(t["set"]["flow"], "classic")

    def test_flow_invalid(self):
        with self.assertRaises(Fail):
            settings.set_task({}, "flow", "waterfall")

    def test_verify_rounds_valid_and_spellings(self):
        for key in ("verify.rounds", "verify-rounds", "verify_rounds"):
            t = {}
            settings.set_task(t, key, "3")
            self.assertEqual(t["set"]["verify_rounds"], 3)
        t = {}
        settings.set_task(t, "verify.rounds", "5 rounds")
        self.assertEqual(t["set"]["verify_rounds"], 5)

    def test_verify_rounds_out_of_range(self):
        for bad in ("0", "6"):
            with self.assertRaises(Fail):
                settings.set_task({}, "verify.rounds", bad)

    def test_retro_valid_and_invalid(self):
        for v in ("off", "ask", "automatic"):
            t = {}
            settings.set_task(t, "retro", v)
            self.assertEqual(t["set"]["retro"], v)
        with self.assertRaises(Fail):
            settings.set_task({}, "retro", "sometimes")

    def test_default_clears_the_set(self):
        t = {}
        settings.set_task(t, "flow", "openspec")
        settings.set_task(t, "flow", "default")
        self.assertNotIn("set", t)

    def test_unknown_key_message_names_flow(self):
        with self.assertRaises(Fail) as e:
            settings.set_task({}, "nope", "x")
        self.assertIn("flow", str(e.exception))


class Block(unittest.TestCase):
    """The Settings block (settings_block / task_settings)."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.file = os.path.join(self.tmp, "shepherdr.json")
        p = mock.patch.object(settings, "SETTINGS", self.file)
        p.start()
        self.addCleanup(p.stop)
        self.s = Store()

    def write(self, d):
        with open(self.file, "w") as f:
            json.dump(d, f)

    def task(self, **kw):
        return {"id": 1, "space": "sp1", "mode": "worktree", **kw}

    def test_classic_default_has_no_flow_or_retro(self):
        block = settings.settings_block(self.s, self.task())
        self.assertEqual(labels(settings.task_settings(self.s, self.task())), ["Review", "Merge", "Proof", "Agent"])
        self.assertNotIn("- Flow:", block)
        self.assertNotIn("- Retro:", block)

    def test_set_flow_openspec_shows_the_flow_line_first(self):
        t = self.task()
        settings.set_task(t, "flow", "openspec")
        lines = settings.task_settings(self.s, t)
        self.assertEqual(lines[0]["label"], "Flow")
        block = settings.settings_block(self.s, t)
        self.assertIn("- Flow: openspec (set by the assistant for this task)", block)

    def test_space_flow_openspec_says_who_set_it(self):
        self.write({"spaces": {"sp1": {"folder": FOLDER, "tasks": {"flow": "openspec"}}}})
        block = settings.settings_block(self.s, self.task())
        self.assertEqual(line(block, "Flow"), "- Flow: openspec (this folder's setting)")

    def test_retro_line_only_when_not_off(self):
        self.write({"everywhere": {"tasks": {"retro": "automatic"}}})
        block = settings.settings_block(self.s, self.task())
        self.assertEqual(line(block, "Retro"), "- Retro: automatic (your setting everywhere)")
        # a per-task --set wins
        t = self.task()
        settings.set_task(t, "retro", "ask")
        self.assertEqual(line(settings.settings_block(self.s, t), "Retro"), "- Retro: ask me (set by the assistant for this task)")

    def test_set_flow_back_to_default_restores_classic_block(self):
        plain = settings.settings_block(self.s, self.task())
        t = self.task()
        settings.set_task(t, "flow", "openspec")
        settings.set_task(t, "flow", "default")
        self.assertEqual(settings.settings_block(self.s, t), plain)


class AssistantBlock(unittest.TestCase):
    """The Assistant's "Your settings" block (assistant_settings.rows / block)."""
    NO_AUTO = (False, "", "")

    def test_default_block_has_no_flow_or_retro_rows(self):
        rs = aset.rows("sp1", {}, self.NO_AUTO)
        keys = [r["key"] for r in rs]
        self.assertNotIn("flow", keys)
        self.assertNotIn("retro", keys)
        self.assertEqual(len(rs), 11)  # unchanged from before the flow settings

    def test_flow_and_retro_rows_appear_when_set(self):
        d = {"everywhere": {"tasks": {"flow": "openspec", "retro": "ask"}}}
        rs = aset.rows("sp1", d, self.NO_AUTO)
        by_key = {r["key"]: r for r in rs}
        self.assertIn("flow", by_key)
        self.assertIn("retro", by_key)
        self.assertTrue(by_key["flow"]["text"].startswith("openspec"))
        self.assertTrue(by_key["retro"]["text"].startswith("ask me"))
        self.assertEqual(by_key["flow"]["by"], "everywhere")
        self.assertEqual(aset.block(rs).count("\n- Flow: "), 1)

    def test_space_sets_retro_says_set_for_this_space(self):
        d = {"spaces": {"sp1": {"tasks": {"retro": "automatic"}}}}
        rs = aset.rows("sp1", d, self.NO_AUTO)
        retro = next(r for r in rs if r["key"] == "retro")
        self.assertEqual(retro["by"], "space")
        self.assertTrue(retro["text"].startswith("automatic"))

    def test_notice_lines_are_well_formed_for_flow_and_retro(self):
        d = {"everywhere": {"tasks": {"flow": "openspec", "retro": "automatic"}}}
        rs = aset.rows("sp1", d, self.NO_AUTO)
        for ln in aset.notice(rs).split("\n"):
            self.assertRegex(ln, r'^<herdr-setting name="\w+" value="[^"]*" scope="\w+">[^\n]*</herdr-setting>$')


if __name__ == "__main__":
    unittest.main()
