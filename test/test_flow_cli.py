"""herdr-task's OpenSpec flow CLI skeleton (cli.py, T20a).

Every new command, subcommand and flag of the contract (docs/openspec-flow.md, C4) parses in a
throwaway root, and each dispatches to the contract's `<module>.cmd_<name>` with a lazy import.
Until the module (built by another task) lands, the command exits 1 with a clear "not available
yet" error. Only the CLI surface is exercised here; the flow modules live in other files.

These commands fail on the lazy import before any side effect, so no hc or gh is ever run and a
throwaway HOME is safe. The `add`/`list` flags are checked through `--help` and argparse, which
also never reach hc.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin", "herdr-task")

# Every flow command, with minimal valid arguments and the module it dispatches to. Each must
# parse (no argparse usage error) and reach its lazy dispatch, which refuses with "not available
# yet" because the module (or the entry function) is not on this branch.
FLOW_COMMANDS = [
    ("feature", ["feature", "add", "Login flow"]),
    ("feature", ["feature", "list"]),
    ("feature", ["feature", "show", "HAT-12"]),
    ("feature", ["feature", "sync", "HAT-12"]),
    ("feature", ["feature", "cancel", "HAT-12"]),
    ("feature", ["feature", "merge", "HAT-12"]),
    ("feature", ["feature", "fix-round", "HAT-12"]),
    ("fanout", ["fanout", "add-login-flow"]),
    ("sync-branch", ["sync-branch"]),
    ("feature-status", ["feature-status", "HAT-12"]),
    ("verify-report", ["verify-report", "--pass"]),
    ("verify-report", ["verify-report", "--findings-file", "findings.json"]),
    ("retro-data", ["retro-data", "HAT-12"]),
    ("retro", ["retro", "HAT-12"]),
    ("flow init", ["flow", "init"]),
]

# The new command and subcommand names that must show up in the top-level help.
TOP_LEVEL = ["feature", "fanout", "sync-branch", "feature-status", "verify-report",
             "retro-data", "retro", "flow"]

FEATURE_SUBS = ["add", "show", "list", "sync", "cancel", "merge", "fix-round"]


def run(*args, folder=None):
    """herdr-task in a throwaway root. The commands under test never reach hc, so a changed HOME
    is safe here (the hc guard only fires when hc is actually invoked)."""
    home = tempfile.mkdtemp()
    env = {**os.environ, "HOME": home, "HERDR_TASK_STORE": os.path.join(home, "tasks")}
    cmd = [sys.executable, BIN, *args]
    if folder is not None:
        cmd += ["--folder", folder]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


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

    def test_every_command_has_help(self):
        for _, argv in FLOW_COMMANDS:
            with self.subTest(argv=argv):
                # The command word(s) plus --help; drop the sample positional/flag arguments.
                head = ["feature"] if argv[0] == "feature" else argv[:1] if argv[0] != "flow" else ["flow", "init"]
                p = run(*head, "--help")
                self.assertEqual(p.returncode, 0, f"{head} --help: {p.stderr}")


class Dispatch(unittest.TestCase):
    """Each command parses and reaches its lazy dispatch, which says "not available yet"."""

    def test_commands_parse_and_report_not_available(self):
        work = tempfile.mkdtemp()
        for module, argv in FLOW_COMMANDS:
            with self.subTest(argv=argv):
                p = run(*argv, folder=work)
                self.assertEqual(p.returncode, 1, f"{argv}: rc {p.returncode}, {p.stderr}")
                self.assertIn("is not available yet", p.stderr, f"{argv}: {p.stderr}")

    def test_not_available_message_names_the_command(self):
        for label, argv in [("feature", ["feature", "list"]), ("fanout", ["fanout", "c"]),
                            ("sync-branch", ["sync-branch"]), ("flow init", ["flow", "init"])]:
            with self.subTest(argv=argv):
                p = run(*argv)
                self.assertIn(f"{label} is not available yet", p.stderr)

    def test_json_refusal_shape(self):
        p = run("fanout", "add-login-flow", "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        got = json.loads(p.stdout)
        self.assertIn("not available yet", got["error"])


class AddFlags(unittest.TestCase):
    """The flow flags on the existing `add` are parsed here (commands.py stores them)."""

    def test_add_help_lists_the_flow_flags(self):
        p = run("add", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for flag in ("--feature", "--role", "--owns", "--risk"):
            self.assertIn(flag, p.stdout, f"add --help does not list {flag}")

    def test_role_choices_are_the_five_roles_plus_retro(self):
        p = run("add", "x", "--role", "nope")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        for role in ("spec", "child", "verify", "fix", "integration", "retro"):
            self.assertIn(role, p.stderr)

    def test_risk_choices_are_low_and_high(self):
        p = run("add", "x", "--risk", "medium")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("low", p.stderr)
        self.assertIn("high", p.stderr)


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
