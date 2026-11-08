"""The work table parser and plan (T05a, contract C3/C5/C6/C7): parse, check, overlap links, waves,
deterministic order, and checkbox ticking. Pure; nothing is mocked. The shared fixtures of
test/fixtures/openspec-flow/ are owned by T00 and may not be on this branch yet, so the fixture
tests skip when the folder is absent; the inline tables cover every rule regardless."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(ROOT, "mods", "tasks", "bin"))
from herdr_task import worktable  # noqa: E402
from herdr_task.worktable import WorkTableError  # noqa: E402

FIXTURES = os.path.join(ROOT, "test", "fixtures", "openspec-flow")

VALID = """# Login flow

## Work table

| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 0.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 1.1 | Parse headers       | src/parser/header.ts | 0.1     | low  | S    |
| 1.2 | Parse body          | src/parser/body.ts   | 0.1     | high | M    |
| 2.1 | Wire parser         | src/parser/index.ts  | 1.1,1.2 | low  | M    |

## Tasks

- [ ] 0.1 Shared parser types
- [ ] 1.1 Parse headers
- [ ] 1.2 Parse body
- [ ] 2.1 Wire parser
"""


def table(body_rows):
    """A minimal work table whose body is the given data rows (each a full `| ... |` line)."""
    head = "| id  | title | owns | depends | risk | size |\n|---|---|---|---|---|---|\n"
    return "## Work table\n\n" + head + "\n".join(body_rows) + "\n"


class Parse(unittest.TestCase):
    def test_valid_rows(self):
        rows = worktable.parse_table(VALID)
        self.assertEqual([r["id"] for r in rows], ["0.1", "1.1", "1.2", "2.1"])
        self.assertEqual(rows[0]["owns"], ["src/parser/types.ts"])
        self.assertEqual(rows[0]["depends"], [])
        self.assertEqual(rows[3]["depends"], ["1.1", "1.2"])
        self.assertEqual(rows[2]["risk"], "high")
        self.assertEqual(rows[3]["size"], "M")
        self.assertEqual(rows[0]["title"], "Shared parser types")

    def test_owns_splits_on_comma(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts, src/b.ts |  | low | S |"]))
        self.assertEqual(rows[0]["owns"], ["src/a.ts", "src/b.ts"])

    def test_empty_owns_parses_as_empty_list(self):
        # The cell is left for check_table to reject; parse just reports it empty.
        rows = worktable.parse_table(table(["| 1.1 | A |  |  | low | S |"]))
        self.assertEqual(rows[0]["owns"], [])

    def test_no_table_is_bad_table(self):
        with self.assertRaises(WorkTableError) as cm:
            worktable.parse_table("# A change\n\nNo table here.\n")
        self.assertEqual(cm.exception.code, worktable.BAD_TABLE)

    def test_wrong_column_count_is_bad_table(self):
        with self.assertRaises(WorkTableError) as cm:
            worktable.parse_table(table(["| 1.1 | A | src/a.ts | low | S |"]))  # five cells
        self.assertEqual(cm.exception.code, worktable.BAD_TABLE)

    def test_divider_must_have_six_cells(self):
        text = ("## Work table\n\n| id | title | owns | depends | risk | size |\n| --- |\n"
                "| 1.1 | A | src/a.ts |  | low | S |\n")
        with self.assertRaises(WorkTableError) as cm:
            worktable.parse_table(text)
        self.assertEqual(cm.exception.code, worktable.BAD_TABLE)

    def test_owns_keeps_empty_members_for_the_check(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts,,src/b.ts |  | low | S |"]))
        self.assertEqual(rows[0]["owns"], ["src/a.ts", "", "src/b.ts"])

    def test_stops_at_blank_line(self):
        text = table(["| 1.1 | A | src/a.ts |  | low | S |"]) + "\nSome prose | with a pipe\n"
        rows = worktable.parse_table(text)
        self.assertEqual([r["id"] for r in rows], ["1.1"])


class Check(unittest.TestCase):
    def good(self):
        return worktable.parse_table(VALID)

    def test_valid_passes(self):
        self.assertIsNone(worktable.check_table(self.good()))

    def test_missing_id(self):
        rows = worktable.parse_table(table(["|  | A | src/a.ts |  | low | S |"]))
        self._code(rows, worktable.MISSING_ID)

    def test_malformed_id(self):
        rows = worktable.parse_table(table(["| 1 | A | src/a.ts |  | low | S |"]))
        self._code(rows, worktable.MISSING_ID)

    def test_duplicate_id(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/a.ts |  | low | S |",
            "| 1.1 | B | src/b.ts |  | low | S |",
        ]))
        self._code(rows, worktable.MISSING_ID)

    def test_unknown_dep(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts | 9.9 | low | S |"]))
        self._code(rows, worktable.UNKNOWN_DEP)

    def test_cycle(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/a.ts | 1.2 | low | S |",
            "| 1.2 | B | src/b.ts | 1.1 | low | S |",
        ]))
        self._code(rows, worktable.CYCLE)

    def test_self_cycle(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts | 1.1 | low | S |"]))
        self._code(rows, worktable.CYCLE)

    def test_empty_owns(self):
        rows = worktable.parse_table(table(["| 1.1 | A |  |  | low | S |"]))
        self._code(rows, worktable.EMPTY_OWNS)

    def test_empty_owns_entry_trailing_comma(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts, |  | low | S |"]))
        self._code(rows, worktable.EMPTY_OWNS)

    def test_empty_owns_entry_double_comma(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts,,src/b.ts |  | low | S |"]))
        self._code(rows, worktable.EMPTY_OWNS)

    def test_size_l(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts |  | low | L |"]))
        self._code(rows, worktable.SIZE_L)

    def test_bad_risk(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts |  | medium | S |"]))
        self._code(rows, worktable.BAD_RISK)

    def test_bad_size(self):
        rows = worktable.parse_table(table(["| 1.1 | A | src/a.ts |  | low | XL |"]))
        self._code(rows, worktable.BAD_RISK)

    def _code(self, rows, code):
        with self.assertRaises(WorkTableError) as cm:
            worktable.check_table(rows)
        self.assertEqual(cm.exception.code, code)
        self.assertTrue(cm.exception.message)


class Overlap(unittest.TestCase):
    def test_same_file_links_later_to_earlier(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/x.ts |  | low | S |",
            "| 1.2 | B | src/x.ts |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[0]["depends"], [])
        self.assertEqual(rows[1]["depends"], ["1.1"])

    def test_glob_overlap(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/parser/types.ts |  | low | S |",
            "| 1.2 | B | src/parser/*.ts |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[1]["depends"], ["1.1"])

    def test_two_globs_sharing_a_file_overlap(self):
        # Neither glob is a literal matching the other, but both match src/parser/types.ts.
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/*/types.ts |  | low | S |",
            "| 1.2 | B | src/parser/*.ts |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[1]["depends"], ["1.1"])

    def test_double_star_overlap(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | web/** |  | low | S |",
            "| 1.2 | B | web/src/App.tsx |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[1]["depends"], ["1.1"])

    def test_distinct_subtrees_do_not_overlap(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | web/generated/** |  | low | S |",
            "| 1.2 | B | web/src/App.tsx |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[1]["depends"], [])

    def test_no_overlap_no_link(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/a.ts |  | low | S |",
            "| 1.2 | B | src/b.ts |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[0]["depends"], [])
        self.assertEqual(rows[1]["depends"], [])

    def test_existing_dependency_is_not_duplicated(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/x.ts |  | low | S |",
            "| 1.2 | B | src/x.ts | 1.1 | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        self.assertEqual(rows[1]["depends"], ["1.1"])

    def test_transitive_dependency_is_not_duplicated(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/x.ts |  | low | S |",
            "| 1.2 | B | src/y.ts | 1.1 | low | S |",
            "| 1.3 | C | src/x.ts | 1.2 | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        # 1.3 overlaps 1.1 (src/x.ts) but already reaches it through 1.2, so no new edge.
        self.assertEqual(rows[2]["depends"], ["1.2"])

    def test_overlap_links_keep_it_acyclic(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/x.ts |  | low | S |",
            "| 1.2 | B | src/x.ts |  | low | S |",
            "| 1.3 | C | src/x.ts |  | low | S |",
        ]))
        worktable.add_overlap_links(rows)
        # No raise: the graph stays acyclic, so checking and waving still work.
        worktable.check_table(rows)
        self.assertEqual(worktable.waves(rows), [["1.1"], ["1.2"], ["1.3"]])


class Waves(unittest.TestCase):
    def test_valid_waves(self):
        rows = worktable.parse_table(VALID)
        self.assertEqual(worktable.waves(rows), [["0.1"], ["1.1", "1.2"], ["2.1"]])

    def test_depth_comes_from_the_graph_not_the_id(self):
        # Ids suggest two waves, but the dependency chain is three deep.
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/a.ts |  | low | S |",
            "| 1.2 | B | src/b.ts | 1.1 | low | S |",
            "| 1.3 | C | src/c.ts | 1.2 | low | S |",
        ]))
        self.assertEqual(worktable.waves(rows), [["1.1"], ["1.2"], ["1.3"]])

    def test_within_wave_keeps_table_order(self):
        rows = worktable.parse_table(table([
            "| 0.1 | base | src/z.ts |  | low | S |",
            "| 1.2 | B | src/b.ts | 0.1 | low | S |",
            "| 1.1 | A | src/a.ts | 0.1 | low | S |",
        ]))
        self.assertEqual(worktable.waves(rows), [["0.1"], ["1.2", "1.1"]])


class Order(unittest.TestCase):
    def test_stable(self):
        rows = worktable.parse_table(VALID)
        self.assertEqual(worktable.order(rows), worktable.order(worktable.parse_table(VALID)))

    def test_larger_size_first_when_chain_ties(self):
        # Wave 1 has 1.1 (S) and 1.2 (M); equal dependent chains, so the M row runs first.
        rows = worktable.parse_table(VALID)
        self.assertEqual(worktable.order(rows), ["0.1", "1.2", "1.1", "2.1"])

    def test_longest_dependent_chain_first(self):
        rows = worktable.parse_table(table([
            "| 0.1 | base | src/z.ts |  | low | S |",
            "| 1.1 | A | src/a.ts | 0.1 | low | S |",
            "| 1.2 | B | src/b.ts | 0.1 | low | S |",
            "| 2.1 | C | src/c.ts | 1.1 | low | S |",
        ]))
        # 1.1 has a dependent (2.1); 1.2 has none, so 1.1 runs first in its wave.
        self.assertEqual(worktable.order(rows), ["0.1", "1.1", "1.2", "2.1"])

    def test_table_order_breaks_full_tie(self):
        rows = worktable.parse_table(table([
            "| 1.1 | A | src/a.ts |  | low | S |",
            "| 1.2 | B | src/b.ts |  | low | S |",
        ]))
        self.assertEqual(worktable.order(rows), ["1.1", "1.2"])


class Checkboxes(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(worktable.checkbox_ids(VALID), ["0.1", "1.1", "1.2", "2.1"])

    def test_ids_read_ticked_lines_too(self):
        md = "- [x] 1.1 Done one\n- [ ] 1.2 Open one\n"
        self.assertEqual(worktable.checkbox_ids(md), ["1.1", "1.2"])

    def test_tick_one_box(self):
        out = worktable.tick(VALID, "1.2")
        self.assertIn("- [x] 1.2 Parse body", out)
        self.assertIn("- [ ] 1.1 Parse headers", out)  # others untouched
        self.assertIn("- [ ] 2.1 Wire parser", out)

    def test_tick_is_idempotent(self):
        once = worktable.tick(VALID, "1.2")
        self.assertEqual(worktable.tick(once, "1.2"), once)

    def test_tick_unknown_id_unchanged(self):
        self.assertEqual(worktable.tick(VALID, "9.9"), VALID)

    def test_tick_preserves_the_rest_of_the_text(self):
        out = worktable.tick(VALID, "0.1")
        self.assertTrue(out.startswith("# Login flow"))
        self.assertEqual(out.count("\n"), VALID.count("\n"))


class Pipeline(unittest.TestCase):
    def test_parse_check_link_wave_order(self):
        rows = worktable.parse_table(VALID)
        worktable.check_table(rows)
        worktable.add_overlap_links(rows)  # distinct files: adds nothing
        self.assertEqual(worktable.waves(rows), [["0.1"], ["1.1", "1.2"], ["2.1"]])
        self.assertEqual(worktable.order(rows)[0], "0.1")

    def test_error_carries_code_and_message(self):
        err = WorkTableError(worktable.CYCLE, "a cycle")
        self.assertEqual(err.code, worktable.CYCLE)
        self.assertEqual(err.message, "a cycle")
        self.assertEqual(str(err), "a cycle")


@unittest.skipUnless(os.path.isdir(FIXTURES), "shared openspec-flow fixtures (T00) not on this branch yet")
class Fixtures(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()

    def test_valid_fixture(self):
        text = self.read("tasks-valid.md")
        rows = worktable.parse_table(text)
        worktable.check_table(rows)
        self.assertEqual([r["id"] for r in rows], ["0.1", "1.1", "1.2", "2.1"])
        self.assertEqual(worktable.waves(rows), [["0.1"], ["1.1", "1.2"], ["2.1"]])
        self.assertEqual(worktable.checkbox_ids(text), ["0.1", "1.1", "1.2", "2.1"])
        self.assertIn("- [x] 2.1 Wire parser", worktable.tick(text, "2.1"))

    def test_invalid_fixtures_raise_the_contract_codes(self):
        cases = {
            "tasks-invalid-cycle.md": worktable.CYCLE,
            "tasks-invalid-unknown-dep.md": worktable.UNKNOWN_DEP,
            "tasks-invalid-empty-owns.md": worktable.EMPTY_OWNS,
            "tasks-invalid-size-l.md": worktable.SIZE_L,
            "tasks-invalid-bad-risk.md": worktable.BAD_RISK,
        }
        for name, code in cases.items():
            with self.subTest(fixture=name):
                rows = worktable.parse_table(self.read(name))
                with self.assertRaises(WorkTableError) as cm:
                    worktable.check_table(rows)
                self.assertEqual(cm.exception.code, code)


if __name__ == "__main__":
    unittest.main()
