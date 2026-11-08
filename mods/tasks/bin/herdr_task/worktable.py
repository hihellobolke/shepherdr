"""The work table of a Feature's change (tasks.md): parse it, check it, link overlapping rows,
compute waves, order the rows deterministically, and tick a row's checkbox by id.

This module is pure (contract C7, task T05a): no git, no store and no herdr-task calls. It fixes
the shared names that `fanout` (T05b) composes into work packages and placement; grouping rows into
packages (section 6.7 step 5) and placing them (step 7) live in `fanout.py`, which builds them from
`waves` and `order` here. The grammar and the error codes are the contract's (docs/openspec-flow.md,
sections C3, C5 and C6); the glossary words (proposal section 2) are the ones used here: work table,
row, wave, chain.
"""

import fnmatch
import re


# ---- error codes (contract C5/C6) -----------------------------------------------------------

BAD_TABLE = "bad-table"
MISSING_ID = "missing-id"
UNKNOWN_DEP = "unknown-dep"
CYCLE = "cycle"
EMPTY_OWNS = "empty-owns"
SIZE_L = "size-l"
BAD_RISK = "bad-risk"


class WorkTableError(Exception):
    """A bad work table. `.code` is one of the contract codes above; `.message` is for the person."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- grammar (contract C3) ------------------------------------------------------------------

_HEADER = ["id", "title", "owns", "depends", "risk", "size"]
_ID_RE = re.compile(r"^\d+\.\d+$")
_RISKS = ("low", "high")
_SIZES = ("S", "M", "L")
_SIZE_RANK = {"S": 0, "M": 1, "L": 2}

# Group 3 is the row id. Matches `- [ ] 2.1 Parse headers` and the ticked `- [x] ...` form.
CHECKBOX_RE = re.compile(r"^(\s*- \[)[ xX](\]\s+)(\d+\.\d+)(\s.*)$")


def _cells(line):
    """A pipe-delimited table line split into stripped cells, without the outer empties."""
    parts = [c.strip() for c in line.split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _is_divider(cells):
    return bool(cells) and all(re.fullmatch(r":?-+:?", c) for c in cells)


def parse_table(text):
    """The rows of the work table in `text`, each {id, title, owns: [...], depends: [...], risk, size}.

    Pulls the strings out of the one table whose header is the six contract columns; it does not
    judge their values (`check_table` does that). Raises WorkTableError(BAD_TABLE) when there is no
    such table or a row does not have six columns.
    """
    lines = text.splitlines()
    header_at = None
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        if [c.lower() for c in _cells(line)] == _HEADER:
            if i + 1 < len(lines) and _is_divider(_cells(lines[i + 1])):
                header_at = i
                break
    if header_at is None:
        raise WorkTableError(
            BAD_TABLE,
            "no work table found (need the | id | title | owns | depends | risk | size | header)",
        )
    rows = []
    for line in lines[header_at + 2:]:
        if line.strip() == "" or not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != 6:
            raise WorkTableError(
                BAD_TABLE,
                f"work table row has {len(cells)} columns, expected 6: {line.strip()}",
            )
        rid, title, owns, depends, risk, size = cells
        rows.append({
            "id": rid,
            "title": title,
            "owns": [p.strip() for p in owns.split(",") if p.strip()],
            "depends": [d.strip() for d in depends.split(",") if d.strip()],
            "risk": risk,
            "size": size,
        })
    return rows


def check_table(rows):
    """Raise WorkTableError for a bad work table: a missing id, an unknown dependency, a cycle, an
    empty owns, a size L row or a bad risk/size value (contract C6). Returns None when the table is
    good. The checks run in the proposal's order (section 6.7 step 2)."""
    seen = set()
    for row in rows:
        rid = row["id"]
        if not rid or not _ID_RE.match(rid):
            raise WorkTableError(MISSING_ID, f"a row has no valid id (expected like 2.1): {row.get('title') or '?'!r}")
        if rid in seen:
            raise WorkTableError(MISSING_ID, f"duplicate row id: {rid}")
        seen.add(rid)
    for row in rows:
        for dep in row["depends"]:
            if dep not in seen:
                raise WorkTableError(UNKNOWN_DEP, f"row {row['id']} depends on {dep}, which is not a row")
    _detect_cycle(rows)
    for row in rows:
        if not row["owns"]:
            raise WorkTableError(EMPTY_OWNS, f"row {row['id']} has an empty owns")
    for row in rows:
        if row["size"] == "L":
            raise WorkTableError(SIZE_L, f"row {row['id']} is size L and must be split")
        if row["size"] not in _SIZES:
            raise WorkTableError(BAD_RISK, f"row {row['id']} has size {row['size']!r}, expected S, M or L")
        if row["risk"] not in _RISKS:
            raise WorkTableError(BAD_RISK, f"row {row['id']} has risk {row['risk']!r}, expected low or high")


def add_overlap_links(rows):
    """Add a dependency between two rows whose owns overlap and that have none, keeping table order
    (the later row depends on the earlier one, so no cycle is introduced). Returns the rows."""
    for j in range(len(rows)):
        for i in range(j):
            a, b = rows[i], rows[j]
            if not _owns_overlap(a["owns"], b["owns"]):
                continue
            if _reachable(rows, b["id"], a["id"]) or _reachable(rows, a["id"], b["id"]):
                continue
            b["depends"].append(a["id"])
    return rows


def waves(rows):
    """The row ids grouped by dependency depth (a wave), by topological sort. A row with no
    dependency is depth 0; otherwise it is one past its deepest dependency. Rows within a wave keep
    table order."""
    depth = _depths(rows)
    order_index = {row["id"]: i for i, row in enumerate(rows)}
    by_depth = {}
    for row in rows:
        by_depth.setdefault(depth[row["id"]], []).append(row["id"])
    return [sorted(by_depth[d], key=lambda rid: order_index[rid]) for d in sorted(by_depth)]


def order(rows):
    """The deterministic run order of the row ids: wave first, then the longest chain of dependents,
    then larger size, then table order. The same table always gives the same order."""
    depth = _depths(rows)
    chain = _dependent_chains(rows)
    order_index = {row["id"]: i for i, row in enumerate(rows)}
    size_of = {row["id"]: row["size"] for row in rows}

    def key(row):
        rid = row["id"]
        return (depth[rid], -chain[rid], -_SIZE_RANK.get(size_of[rid], 0), order_index[rid])

    return [row["id"] for row in sorted(rows, key=key)]


def checkbox_ids(tasks_md):
    """The row ids from the `- [ ] <id> ...` checkbox lines of a tasks.md, in order."""
    ids = []
    for line in tasks_md.splitlines():
        m = CHECKBOX_RE.match(line)
        if m:
            ids.append(m.group(3))
    return ids


def tick(tasks_md, row_id):
    """The tasks.md text with the checkbox of `row_id` ticked (`- [ ]` becomes `- [x]`). Other lines
    are untouched; a text that does not hold the row comes back unchanged."""
    lines = tasks_md.split("\n")
    for idx, line in enumerate(lines):
        m = CHECKBOX_RE.match(line)
        if m and m.group(3) == row_id:
            lines[idx] = m.group(1) + "x" + m.group(2) + m.group(3) + m.group(4)
    return "\n".join(lines)


# ---- internals ------------------------------------------------------------------------------

def _owns_overlap(a, b):
    """True when two owns lists could touch the same path: an equal entry, or one entry matches the
    other as a glob."""
    for pa in a:
        for pb in b:
            if pa == pb or fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                return True
    return False


def _reachable(rows, start, target):
    """True when `start` transitively depends on `target` through the depends edges."""
    graph = {row["id"]: row["depends"] for row in rows}
    stack = list(graph.get(start, []))
    seen = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return False


def _detect_cycle(rows):
    """Raise WorkTableError(CYCLE) when the depends edges hold a cycle."""
    graph = {row["id"]: row["depends"] for row in rows}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in graph}

    def visit(node):
        color[node] = GRAY
        for dep in graph[node]:
            if dep not in color:
                continue
            if color[dep] == GRAY:
                raise WorkTableError(CYCLE, f"dependency cycle involving {node} and {dep}")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for rid in graph:
        if color[rid] == WHITE:
            visit(rid)


def _depths(rows):
    """Each row id to its dependency depth; raises WorkTableError(CYCLE) on a cycle."""
    graph = {row["id"]: row["depends"] for row in rows}
    depth = {}
    visiting = set()

    def compute(node):
        if node in depth:
            return depth[node]
        if node in visiting:
            raise WorkTableError(CYCLE, f"dependency cycle at {node}")
        visiting.add(node)
        deps = [dep for dep in graph.get(node, []) if dep in graph]
        val = 0 if not deps else 1 + max(compute(dep) for dep in deps)
        visiting.discard(node)
        depth[node] = val
        return val

    for rid in graph:
        compute(rid)
    return depth


def _dependent_chains(rows):
    """Each row id to the length of the longest chain of rows that depend on it (downstream)."""
    dependents = {row["id"]: [] for row in rows}
    for row in rows:
        for dep in row["depends"]:
            if dep in dependents:
                dependents[dep].append(row["id"])
    chain = {}
    visiting = set()

    def compute(node):
        if node in chain:
            return chain[node]
        if node in visiting:
            raise WorkTableError(CYCLE, f"dependency cycle at {node}")
        visiting.add(node)
        kids = dependents.get(node, [])
        val = 0 if not kids else 1 + max(compute(k) for k in kids)
        visiting.discard(node)
        chain[node] = val
        return val

    for rid in dependents:
        compute(rid)
    return chain
