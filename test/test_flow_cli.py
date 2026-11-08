"""herdr-task's OpenSpec flow CLI skeleton (cli.py, T20a).

Every new command, subcommand and flag of the contract (docs/openspec-flow.md, C4) parses, and each
dispatches to the contract's `<module>.cmd_<name>` with a lazy import. Until the module (built by
another task) lands, the command exits with a clear "not available yet" error. Only the CLI surface
is exercised here; the flow modules live in other files.

Two things keep this test honest across the parallel work:

- The lazy-dispatch contract itself (the "not available yet" fallback, and that a real import error
  is not masked by it) is unit-tested against `cli.flow` directly, so it holds whether or not the
  sibling flow modules have landed in the tree. The end-to-end "not available yet" check runs only
  for the commands whose target module is genuinely absent in the current tree, so it does not
  break (nor run real flow operations) once those modules land.

- HOME is never changed (AGENTS.md: herdr-task must not run with a changed HOME). The commands under
  test never reach hc or the real store: they fail on the lazy import, print `--help`, or hit an
  argparse usage error, all before any side effect. Where a command reads the store, the test points
  HERDR_TASK_STORE and `--folder` at throwaway directories and leaves HOME alone.
"""
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

# Each flow command: the module and entry function it must dispatch to (C4/C7), and a minimal valid
# invocation. The dispatch target is the contract's, so this table is also the list of wirings.
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

# The new command and subcommand names that must show up in the top-level help.
TOP_LEVEL = ["feature", "fanout", "sync-branch", "feature-status", "verify-report",
             "retro-data", "retro", "flow"]
FEATURE_SUBS = ["add", "show", "list", "sync", "cancel", "merge", "fix-round"]

# The command words (not the sample arguments) for each distinct command, for the --help checks.
HELP_PATHS = [["feature", s] for s in FEATURE_SUBS] + [
    ["fanout"], ["sync-branch"], ["feature-status"], ["verify-report"],
    ["retro-data"], ["retro"], ["flow", "init"],
]


def available(module, func):
    """Whether this flow module and its entry function have landed in the current tree. On this
    task's branch none have, so the fallback is exercised; on an integrated tree a landed command's
    success path is that module's own test, not this one's."""
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


class Help(unittest.TestCase):
    def test_top_level_help_lists_the_commands(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in TOP_LEVEL:
            self.assertIn(name, p.stdout, f"--help does not list {name}")

    def test_feature_help_lists_its_subcommands(self):
        p = run("feature", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in FEATURE_SUBS:
            self.assertIn(name, p.stdout, f"feature --help does not list {name}")

    def test_each_command_and_subcommand_has_help(self):
        for path in HELP_PATHS:
            with self.subTest(path=path):
                p = run(*path, "--help")
                self.assertEqual(p.returncode, 0, f"{path} --help: {p.stderr}")


class Dispatch(unittest.TestCase):
    def test_every_command_parses_and_reaches_dispatch(self):
        # A parsed command never exits 2 (argparse's usage-error code); it reaches its handler.
        work = tempfile.mkdtemp()
        for _, _, argv in COMMANDS:
            with self.subTest(argv=argv):
                p = run(*argv, folder=work)
                self.assertNotEqual(p.returncode, 2, f"{argv} failed to parse: {p.stderr}")

    def test_absent_modules_report_not_available(self):
        # End-to-end proof of the wiring for every command whose module has not landed in this tree.
        work = tempfile.mkdtemp()
        for module, func, argv in COMMANDS:
            if available(module, func):
                continue
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


class AddFlags(unittest.TestCase):
    """The flow flags on the existing `add` are parsed here (commands.py stores them)."""

    def test_add_help_lists_the_flow_flags(self):
        p = run("add", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for flag in ("--feature", "--role", "--owns", "--risk"):
            self.assertIn(flag, p.stdout, f"add --help does not list {flag}")

    def test_role_choices_are_the_contract_roles(self):
        p = run("add", "x", "--role", "nope")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        for role in ("spec", "child", "verify", "fix", "integration", "retro"):
            self.assertIn(role, p.stderr)

    def test_risk_choices_are_low_and_high(self):
        p = run("add", "x", "--risk", "medium")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("low", p.stderr)
        self.assertIn("high", p.stderr)


class OptionalFlagsParse(unittest.TestCase):
    """The optional flags parse (rc != 2 is "no argparse usage error"), on this branch or a landed
    tree alike. None of these commands use exit code 2 for their own outcomes."""

    CASES = [
        ["feature", "add", "A feature", "--base", "main", "--change", "add-x", "--mode", "local", "--no-auto-fanout"],
        ["fanout", "add-x", "--dry-run", "--machines", "vm-a,vm-b", "--amend", "--no-start"],
        ["feature-status", "HAT-12", "--on", "vm-b"],
        ["feature", "show", "HAT-12", "--on", "vm-b"],
        ["verify-report", "--findings-file", "f.json", "--feature", "HAT-12"],
        ["flow", "init", "--update"],
        ["sync-branch", "--base", "feature/HAT-12-x"],
    ]

    def test_optional_flags_parse(self):
        work = tempfile.mkdtemp()
        for argv in self.CASES:
            with self.subTest(argv=argv):
                p = run(*argv, folder=work)
                self.assertNotEqual(p.returncode, 2, f"{argv} failed to parse: {p.stderr}")
                self.assertNotIn("unrecognized arguments", p.stderr, f"{argv}: {p.stderr}")
                self.assertNotIn("invalid choice", p.stderr, f"{argv}: {p.stderr}")


class ListFilter(unittest.TestCase):
    """The `list --feature` filter is parsed here (commands.py applies it)."""

    def test_list_help_lists_the_feature_filter(self):
        p = run("list", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--feature", p.stdout)

    def test_list_feature_parses_in_a_throwaway_root(self):
        work = tempfile.mkdtemp()
        p = run("list", "--feature", "HAT-12", folder=work)
        self.assertEqual(p.returncode, 0, p.stderr)


class VerifyReport(unittest.TestCase):
    def test_requires_pass_or_findings_file(self):
        p = run("verify-report")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--pass", p.stderr)
        self.assertIn("--findings-file", p.stderr)


if __name__ == "__main__":
    unittest.main()
