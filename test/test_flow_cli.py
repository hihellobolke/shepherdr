"""herdr-task's OpenSpec flow CLI skeleton (cli.py, T20a).

Every new command, subcommand and flag of the contract (docs/openspec-flow.md, C4) parses, and each
dispatches to the contract's `<module>.cmd_<name>` with a lazy import. Until the module (built by
another task) lands, the command exits with a clear "not available yet" error. Only the CLI surface
is exercised here; the flow modules live in other files.

The tests never execute a command's handler:

- Parsing, the flags and the exact dispatch target are checked in-process against `cli.build_parser`
  and the `flow_target` each dispatcher carries. This runs no handler, so it is safe and stable
  whether or not the sibling flow modules have landed, and it never reaches a machine, a skill
  update or the store.
- The lazy-dispatch contract itself (the "not available yet" fallback, and that a real import error
  is not masked by it) is unit-tested against `cli.flow` directly.
- The one end-to-end check that does run the binary exercises only the "not available yet" fallback,
  and only for the commands whose target module is genuinely absent in the current tree (so it does
  not run real flow operations once those modules land). It leaves HOME untouched (AGENTS.md: no
  changed HOME) and points HERDR_TASK_STORE and --folder at throwaways; these commands fail on the
  lazy import before any side effect, so no hc or gh is reached.
"""
import contextlib
import io
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
ENTRY = os.path.join(BIN, "herdr-task")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
from herdr_task import cli  # noqa: E402
from herdr_task.base import Fail  # noqa: E402

PARSER = cli.build_parser()

TOP_LEVEL = ["feature", "fanout", "sync-branch", "feature-status", "verify-report",
             "retro-data", "retro", "flow"]
FEATURE_SUBS = ["add", "show", "list", "sync", "cancel", "merge", "fix-round"]

# Each flow command: the (module, function) it must dispatch to (C4/C7), and a minimal invocation
# with no side-effect flags (no --on, no --update), used for the end-to-end fallback check.
COMMANDS = [
    ("feature", "cmd_feature", ["feature", "add", "Login flow"]),
    ("feature", "cmd_feature", ["feature", "list"]),
    ("feature", "cmd_feature", ["feature", "show", "HAT-12"]),
    ("feature", "cmd_feature", ["feature", "sync", "HAT-12"]),
    ("feature", "cmd_feature", ["feature", "cancel", "HAT-12"]),
    ("feature", "cmd_feature", ["feature", "merge", "HAT-12"]),
    ("feature", "cmd_feature", ["feature", "fix-round", "HAT-12"]),
    ("fanout", "cmd_fanout", ["fanout", "add-login-flow"]),
    ("branchsync", "cmd_sync_branch", ["sync-branch"]),
    ("feature_status", "cmd_feature_status", ["feature-status", "HAT-12"]),
    ("commands", "cmd_verify_report", ["verify-report", "--pass"]),
    ("commands", "cmd_verify_report", ["verify-report", "--findings-file", "f.json"]),
    ("retro_data", "cmd_retro_data", ["retro-data", "HAT-12"]),
    ("retro", "cmd_retro", ["retro", "HAT-12"]),
    ("flow_init", "cmd_flow_init", ["flow", "init"]),
]


def parse(argv):
    return PARSER.parse_args(argv)


def parse_error(argv):
    """The argparse exit code for a usage error (2), with its message swallowed."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            PARSER.parse_args(argv)
        except SystemExit as e:
            return e.code
    return None


def available(module, func):
    """Whether this flow module and its entry function have landed in the current tree. On this
    task's branch none have."""
    try:
        mod = importlib.import_module(f"herdr_task.{module}")
    except ModuleNotFoundError as e:
        if e.name in (module, f"herdr_task.{module}"):
            return False
        raise
    return hasattr(mod, func)


def run(*args, folder=None):
    """herdr-task in a subprocess, HOME left as it is. HERDR_TASK_STORE is a throwaway so the real
    store is never read or written; these commands never reach hc regardless."""
    env = {**os.environ, "HERDR_TASK_STORE": tempfile.mkdtemp()}
    cmd = [sys.executable, ENTRY, *args]
    if folder is not None:
        cmd += ["--folder", folder]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class Dispatcher(unittest.TestCase):
    """cli.flow(): a lazy import with a clear fallback, deterministic whatever the tree holds."""

    def test_missing_module_is_not_available(self):
        with self.assertRaises(Fail) as cm:
            cli.flow("no_such_flow_module", "cmd_x", "demo")(object())
        self.assertEqual(str(cm.exception), "demo is not available yet")

    def test_missing_function_is_not_available(self):
        # herdr_task.base exists but has no cmd_nope: a landed module without its entry yet.
        with self.assertRaises(Fail) as cm:
            cli.flow("base", "cmd_nope", "demo")(object())
        self.assertEqual(str(cm.exception), "demo is not available yet")

    def test_present_module_and_function_dispatches(self):
        mod = types.ModuleType("herdr_task.fake_flow")
        seen = {}
        mod.cmd_go = lambda args: (seen.setdefault("args", args), "done")[1]
        sys.modules["herdr_task.fake_flow"] = mod
        try:
            got = cli.flow("fake_flow", "cmd_go", "demo")("ARGS")
        finally:
            del sys.modules["herdr_task.fake_flow"]
        self.assertEqual(got, "done")
        self.assertEqual(seen["args"], "ARGS")

    def test_real_import_error_is_not_masked(self):
        # A landed module whose own import fails for a different missing dependency must propagate,
        # not be swallowed as "not available yet".
        orig = cli.importlib.import_module

        def fake(name, package=None):
            raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

        cli.importlib.import_module = fake
        try:
            with self.assertRaises(ModuleNotFoundError):
                cli.flow("fanout", "cmd_fanout", "fanout")(object())
        finally:
            cli.importlib.import_module = orig

    def test_dispatcher_carries_its_target(self):
        self.assertEqual(cli.flow("fanout", "cmd_fanout", "fanout").flow_target, ("fanout", "cmd_fanout"))


class DispatchTargets(unittest.TestCase):
    """Each command parses to the contract's (module, function) dispatch target, checked without
    running anything."""

    def test_targets(self):
        for module, func, argv in COMMANDS:
            with self.subTest(argv=argv):
                args = parse(argv)
                self.assertEqual(getattr(args.fn, "flow_target", None), (module, func))

    def test_feature_subcommand_is_recorded(self):
        for sub in FEATURE_SUBS:
            argv = ["feature", "add", "T"] if sub == "add" else ["feature", sub] + ([] if sub == "list" else ["HAT-1"])
            with self.subTest(sub=sub):
                self.assertEqual(parse(argv).feature_cmd, sub)

    def test_add_and_list_keep_their_classic_handlers(self):
        self.assertIs(parse(["add", "t"]).fn, cli.cmd_add)
        self.assertIs(parse(["list"]).fn, cli.cmd_list)


class Flags(unittest.TestCase):
    """Every C4 flag parses to the expected value, in-process."""

    def test_feature_add_flags(self):
        a = parse(["feature", "add", "Login", "--base", "dev", "--change", "add-login", "--mode", "local", "--no-auto-fanout"])
        self.assertEqual((a.title, a.base, a.change, a.mode, a.auto_fanout), ("Login", "dev", "add-login", "local", False))

    def test_feature_add_auto_fanout_defaults_true(self):
        self.assertTrue(parse(["feature", "add", "Login"]).auto_fanout)

    def test_feature_show_and_status_take_on(self):
        self.assertEqual(parse(["feature", "show", "HAT-1", "--on", "vm-b"]).on, "vm-b")
        self.assertEqual(parse(["feature-status", "HAT-1", "--on", "vm-b"]).on, "vm-b")

    def test_fanout_flags(self):
        a = parse(["fanout", "add-x", "--dry-run", "--machines", "a,b,c", "--amend", "--no-start"])
        self.assertEqual((a.change, a.dry_run, a.machines, a.amend, a.start), ("add-x", True, "a,b,c", True, False))

    def test_fanout_defaults(self):
        a = parse(["fanout", "add-x"])
        self.assertEqual((a.dry_run, a.machines, a.amend, a.start), (False, None, False, True))

    def test_sync_branch_base(self):
        self.assertEqual(parse(["sync-branch", "--base", "feature/HAT-12-x"]).base, "feature/HAT-12-x")

    def test_verify_report_pass_and_findings(self):
        a = parse(["verify-report", "--findings-file", "f.json", "--feature", "HAT-1"])
        self.assertEqual((a.findings_file, a.feature, a.passed), ("f.json", "HAT-1", False))
        self.assertTrue(parse(["verify-report", "--pass"]).passed)

    def test_flow_init_update(self):
        self.assertTrue(parse(["flow", "init", "--update"]).update)
        self.assertFalse(parse(["flow", "init"]).update)

    def test_retro_refs(self):
        self.assertEqual(parse(["retro-data", "HAT-7"]).ref, "HAT-7")
        self.assertEqual(parse(["retro", "HAT-7"]).ref, "HAT-7")

    def test_add_flow_flags(self):
        a = parse(["add", "t", "--feature", "HAT-1", "--role", "child", "--owns", "a,b", "--owns", "c", "--risk", "high"])
        self.assertEqual((a.feature, a.role, a.owns, a.risk), ("HAT-1", "child", ["a,b", "c"], "high"))

    def test_list_feature_filter(self):
        self.assertEqual(parse(["list", "--feature", "HAT-9"]).feature, "HAT-9")


class BadUsage(unittest.TestCase):
    """Invalid input is a usage error (exit code 2), before any dispatch."""

    def test_invalid_role(self):
        self.assertEqual(parse_error(["add", "t", "--role", "nope"]), 2)

    def test_invalid_risk(self):
        self.assertEqual(parse_error(["add", "t", "--risk", "medium"]), 2)

    def test_invalid_feature_mode(self):
        self.assertEqual(parse_error(["feature", "add", "t", "--mode", "hybrid"]), 2)

    def test_verify_report_requires_one_of_the_group(self):
        self.assertEqual(parse_error(["verify-report"]), 2)

    def test_verify_report_group_is_exclusive(self):
        self.assertEqual(parse_error(["verify-report", "--pass", "--findings-file", "f.json"]), 2)


class Help(unittest.TestCase):
    def _help(self, words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                PARSER.parse_args(words + ["--help"])
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        return out.getvalue()

    def test_top_level_help_lists_the_commands(self):
        out = self._help([])
        for name in TOP_LEVEL:
            self.assertIn(name, out, f"--help does not list {name}")

    def test_feature_help_lists_its_subcommands(self):
        out = self._help(["feature"])
        for name in FEATURE_SUBS:
            self.assertIn(name, out, f"feature --help does not list {name}")

    def test_each_command_has_help(self):
        paths = [["feature", s] for s in FEATURE_SUBS] + [
            ["fanout"], ["sync-branch"], ["feature-status"], ["verify-report"],
            ["retro-data"], ["retro"], ["flow", "init"], ["add"], ["list"],
        ]
        for words in paths:
            with self.subTest(words=words):
                self.assertTrue(self._help(words))

    def test_add_help_lists_the_flow_flags(self):
        out = self._help(["add"])
        for flag in ("--feature", "--role", "--owns", "--risk"):
            self.assertIn(flag, out, f"add --help does not list {flag}")

    def test_list_help_lists_the_feature_filter(self):
        self.assertIn("--feature", self._help(["list"]))


class EndToEnd(unittest.TestCase):
    """The installed entry point runs, and the lazy dispatch reports absent modules end to end."""

    def test_binary_help_lists_commands(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in TOP_LEVEL:
            self.assertIn(name, p.stdout)

    def test_absent_modules_report_not_available(self):
        work = tempfile.mkdtemp()
        for module, func, argv in COMMANDS:
            if available(module, func):
                continue  # landed in this tree; its success path is that module's own test
            with self.subTest(argv=argv):
                p = run(*argv, folder=work)
                self.assertEqual(p.returncode, 1, f"{argv}: rc {p.returncode}, {p.stderr}")
                self.assertIn("is not available yet", p.stderr, f"{argv}: {p.stderr}")

    def test_json_refusal_shape(self):
        if available("fanout", "cmd_fanout"):
            self.skipTest("fanout has landed in this tree")
        p = run("fanout", "add-login-flow", "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("not available yet", json.loads(p.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
