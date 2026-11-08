# OpenSpec flow

The approved proposal is in [docs/openspec-flow-proposal.md](openspec-flow-proposal.md).
Use its glossary (section 2) for every term in this file: Task, Feature, Ref, Spec Task, Child Task, Verify Task, Fix Task, Integration Task, work table, wave, phase, fix round, closing, digest, retro.

This file begins with the contract.
The contract is owned by task T00 (SHEP-1).
The overview, the commands, the modes, the settings and the how-to follow the contract.
The scale section is added later by task T25.
Those sections come after the contract and do not change it.

<!-- CONTRACT START (T00 owns everything between CONTRACT START and CONTRACT END) -->

## Contract

This section fixes every shared name, field, JSON shape, exit code and cross-module signature before the parallel tasks start.
Code against it exactly.
A name here is the one name.
Keys in `~/.herdr/shepherdr.json` and in the store file are `snake_case`, and the same spelling is read by Python (`settings.py`, `store.py`) and by JavaScript (`core/settings.js`, the service, the web app).
Python modules without a path live in `mods/tasks/bin/herdr_task/`.
If the contract is wrong or missing something a task needs, that task stops and asks for a contract change; it does not invent a name.

### C1. Settings

All new settings work for a space and for everywhere, like the existing task settings.
They default to off or to the safe value.
They live under the `tasks` section of `~/.herdr/shepherdr.json`, except the retro agent, which is a role section of its own (`retro`), next to `worker` and `reviewer`.

| Key (JSON path) | Values | Default | Meaning |
|---|---|---|---|
| `tasks.flow` | `"classic"`, `"openspec"` | `"classic"` | The flow of a space or a Task. `openspec` enables the Feature flow; it does not turn every Task into a Feature. |
| `tasks.verify_rounds` | integer 1 to 5 | `2` | Fix rounds a Feature allows before it shows Needs you. |
| `tasks.risky_paths` | list of glob strings | `[]` | Per-space globs. A row whose `owns` matches one is high risk. |
| `tasks.owns_exempt` | list of glob strings | `[]` | Globs added to the built-in exempt list for the owned-paths check (C6). |
| `tasks.retro` | `"off"`, `"ask"`, `"automatic"` | `"off"` | Whether a retro runs after a Feature is `done`. `ask`: the Assistant asks first. `automatic`: it starts by itself. |
| `tasks.retro_prompt` | `"built-in"`, or a skill name, or a file path | `"built-in"` | The retro instructions. |
| `tasks.retro_save` | `"outside"`, `"repo"`, `"pr"` | `"outside"` | Where the retro report is saved. `repo`: `docs/retros/`. `pr`: a pull request comment. |
| `tasks.may_change` | list, members from `"agents_md"`, `"specs"` | `[]` | What a flow agent may change without the person's Accept. `"agents_md"`: `AGENTS.md`. `"specs"`: specs and ADRs. Empty: nothing. |
| `retro.agent`, `retro.model`, `retro.effort` | as a role (same shape as `worker`/`reviewer`) | `{}` | The retro agent. Empty: the Reviewer's agent, model and effort. |

Python (`settings.py`):
- Add the `tasks.*` keys above to `DEFAULTS["tasks"]`.
- Add a new role section: `DEFAULTS["retro"] = {}`.
- `settings_for` returns the new values for each space, most specific layer wins, exactly as it does now.
- Add `flow` and `verify_rounds` and `retro` (mode) and `must_fix`-style spellings to `SET_KEYS` so `--set` reaches them: `"flow"` and `"brief.flow"` set `flow`; `"verify.rounds"`, `"verify-rounds"`, `"verify_rounds"` set `verify_rounds`; `"retro"` sets the mode. `risky_paths`, `owns_exempt`, `may_change`, `retro_prompt`, `retro_save` and the `retro` role are space settings, not per-Task `--set` values.
- `set_task` validates: `flow` in {`classic`, `openspec`}; `verify_rounds` an int 1 to 5; `retro` in {`off`, `ask`, `automatic`}.

The Settings block that each brief ends with (`task_settings`, `settings_block`):
- When `flow` is `openspec`, add the line `- Flow: openspec`.
- When `flow` is `classic`, the block does not change at all (a classic brief stays byte for byte the same, proven by T15).
- When `tasks.retro` is not `off`, add a `- Retro:` line that says `off`, `ask me` or `automatic` in the same style as the other rows.
- Constants for the words: `FLOW_WORDS = {"classic": "classic", "openspec": "openspec"}` and `RETRO_WORDS = {"off": "off", "ask": "ask me", "automatic": "automatic"}`.

JavaScript (`core/settings.js`, its copies made by `bin/sync-shared`):
- Add the same keys and defaults to `DEFAULTS.tasks`, and `DEFAULTS.retro = {}`.
- Add `ABOUT` entries keyed `"tasks.flow"`, `"tasks.verify_rounds"`, `"tasks.risky_paths"`, `"tasks.retro"`, `"tasks.retro_prompt"`, `"tasks.retro_save"`, `"tasks.may_change"`.
- Add `OLD_NAMES` entries for each new field (`flow: "Flow"`, and so on).
- Add choice constants: `FLOW = [["classic", "Classic"], ["openspec", "OpenSpec flow"]]`; `VERIFY_ROUNDS = [[1, "1 round"], [2, "2 rounds"], [3, "3 rounds"], [4, "4 rounds"], [5, "5 rounds"]]`; `RETRO = [["off", "Off"], ["ask", "Ask me"], ["automatic", "Automatic"]]`; `RETRO_SAVE = [["outside", "Outside the repo"], ["repo", "In the repo (docs/retros/)"], ["pr", "Pull request comment"]]`; `MAY_CHANGE = [["agents_md", "AGENTS.md"], ["specs", "Specs and ADRs"]]`.
- Add `TASK_ROWS` entries for `flow`, `verify_rounds`, `retro`, `retro_save` and `may_change`; `risky_paths` and `owns_exempt` are list editors, not segment rows.
- `lib/coordinator.js` `SHEPHERDR_DEFAULTS` gets the same `tasks.*` defaults so the Assistant reads them the same way.

### C2. The Feature record and the Task fields

#### The Feature record

A Feature is a record in the store of its folder (ADR-0001).
It is not a Task.
It lives in a new `features` list in the store file.
Old code reads a store file that has a `features` list and ignores it (it keys on `tasks`).
A Feature draws its number from the same folder counter (`next`) that Tasks use, so a ref is a Feature or a Task, never both (ADR-0002).

```json
{
  "id": 12,
  "title": "Login flow",
  "change": "add-login-flow",
  "branch": "feature/HAT-12-login-flow",
  "base": "main",
  "mode": "remote",
  "phase": "building",
  "attention": "",
  "spec_task": 13,
  "children": [
    { "ref": 14, "machine": null, "package": "2.1", "rows": ["2.1"], "role": "child" },
    { "ref": 15, "machine": "vm-b", "package": "2.2", "rows": ["2.2"], "role": "child" }
  ],
  "verify_task": 30,
  "fix_rounds": 1,
  "auto_fanout": true,
  "digests": { "local:14": { "...": "the digest of HAT-14, see C5" } },
  "created": "2026-11-07T10:00:00Z"
}
```

Field notes:
- `id`: the integer from the folder counter. `ref` is computed as `<prefix>-<id>` (for example `HAT-12`); it is not stored, exactly as a Task's ref is computed by `Store.ref`.
- `title`, `change`: the title, and the name of the OpenSpec change folder under `openspec/changes/`.
- `branch`: `feature/<SLUG>-<NUM>-<short-title>`. `base`: the branch the Feature merges into, normally `main`.
- `mode`: `"remote"` or `"local"`, fixed at creation (C7 `feature.detect_mode`).
- `phase`: one of the seven phases below. `attention`: a reason code for Needs you (below), or `""`.
- `spec_task`: the integer id of the first spec Task.
- `children`: one entry per child Task, each `{ ref, machine, package, rows, role }`. `machine` is `null` for this (home) machine. `role` is one of the Task roles below.
- `verify_task`: the id of the current or last Verify Task (its stored result is where open findings and the pass flag live, see the Task fields below). The Feature's open findings are the `findings` of this Verify Task.
- `fix_rounds`: the number of fix rounds used.
- `auto_fanout`: whether Plan approval triggers fan-out by itself. Default `true`. `feature add --no-auto-fanout` sets it `false`, and `tick.py` honours it.
- `digests`: a map from a child's machine-qualified key to its digest (C5). The key is `digest.child_key(child)`, which is `"<machine>:<id>"` with `"local"` for the home machine. A child id is unique only within its own machine's store (each machine's folder counter is separate), so the digest key is qualified by machine to keep two machines' children apart.

Phases (the value of `phase`), exactly the seven of proposal section 2.3:

| Phase | Meaning |
|---|---|
| `planning` | The spec Task exists and is not accepted. |
| `building` | Plan approval is done. Children run. The Feature has no agent of its own. |
| `verifying` | All children are merged or skipped. A Verify Task runs. |
| `closing` | Verify is clean. `herdr-task` runs the closing steps (C7 `closing`). |
| `ready` | The pull request is open. The person presses Merge feature. |
| `done` | The pull request is merged. |
| `cancelled` | The person cancelled the Feature. |

Attention reason codes (the value of `attention`, `""` when there is none):
`"verify-open"` (open findings after the allowed fix rounds), `"row-no-child"` (a row has no merged child at closing), `"child-failed"`, `"child-stalled"`, `"needs-plan"` (a child asked for a plan change), `"needs-owns"` (a child asked for more owned paths), `"base-conflict"` (the base will not merge; an Integration Task is needed), `"blocker-finding"` (a Verify Task reported a blocker the Assistant cannot fix).

#### The Task fields

A flow Task is an ordinary Task with extra fields.
A classic Task has none of them, and every flow check is a no-op for it.

| Field | Type | Meaning |
|---|---|---|
| `feature` | integer | The Feature id this Task belongs to. Absent on a classic Task. |
| `role` | string | `"spec"`, `"child"`, `"verify"`, `"fix"`, `"integration"` or `"retro"`. Absent on a classic Task. |
| `owns` | list of strings | The owned paths (paths or globs). The owned-paths guard uses it. Absent or `[]` on a classic Task. |
| `risk` | string | `"low"` or `"high"`. A `high` Task gets `merge=ask`. |
| `rows` | list of strings | The work-table row ids this child does (its work package). |
| `retries` | integer | Restarts used. At most 1 (C7 `phases.should_retry`). |
| `skipped` | boolean | `true` when the row was marked skipped. A skipped row unblocks its dependents. |
| `digest` | object | The digest written when the Task ends (C5). |
| `verify_pass` | boolean | On a `role="verify"` Task only: `true` when `verify-report --pass` was recorded, `false` when findings were recorded, absent before the report. |
| `findings` | list of objects | On a `role="verify"` Task only: the findings recorded by `verify-report --findings-file` (the findings shape of C5). The Feature's open findings are this list of its current Verify Task. |

Review rounds (recorded by `herdr-task reviewed`) gain one field:
- `patch_id`: the git patch-id of the whole diff against the base, stored next to the existing commit hash on each recorded round (C6, C7 `ready_guards`).

### C3. The work table grammar

The spec worker writes the work table into `openspec/changes/<change>/tasks.md`.
`fanout` reads it.
The format is strict.

The table has exactly these six columns, in this order, with a header row and a divider row:

```
| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 1.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 2.1 | Parse headers       | src/parser/header.ts | 1.1     | low  | S    |
| 2.2 | Parse body          | src/parser/body.ts   | 1.1     | high | M    |
```

Rules:
- `id` matches `^\d+\.\d+$` (a wave number, a dot, a row number), for example `2.1`. It is unique in the table.
- `title` is free text, no pipe character.
- `owns` is a comma-separated list of paths or globs, at least one, no empty entry.
- `depends` is a comma-separated list of row ids, or empty. Every id must be a row in the table.
- `risk` is exactly `low` or `high`.
- `size` is exactly `S`, `M` or `L`. A row of size `L` is rejected and must be split.
- Wave 0 (rows with id `0.x`) holds shared contracts: types, interfaces, schemas and test seams.

The checkbox line that each row id matches:
- `tasks.md` has one checkbox line per row, of the form `- [ ] <id> <title>` (for example `- [ ] 2.1 Parse headers`).
- The id after `- [ ]` (or `- [x]`) is the same id as the table row.
- Closing ticks a box by changing `- [ ]` to `- [x]` on the line whose id matches a merged child's row (C7 `closing`, `worktable.tick`).
- The checkbox regex is `^(\s*- \[)[ xX](\]\s+)(\d+\.\d+)(\s.*)$`; group 3 is the id.

### C4. CLI surface

Every new command, subcommand and flag.
Each takes `--folder <path>` (default: here) and `--json`, as the existing commands do, unless said otherwise.
With `--json`, a command prints one JSON object to stdout; the shapes are in C5.
A refusal prints `{"error": "..."}` (and, for the flow guards, a `reason` code) with `--json`, or `herdr-task: <message>` to stderr without it; the exit code is in C6.

`feature` (subcommands dispatch to `feature.cmd_feature`):
- `feature add "<title>" [--base <branch>] [--change <name>] [--mode remote|local] [--no-auto-fanout]`
  Creates the Feature, the feature branch and the spec Task. Prints the Feature record. Prints `No GitHub remote: local mode, one machine only` first when the repo has no GitHub remote. `--change` overrides the generated change name. `--no-auto-fanout` turns off the automatic fan-out on Plan approval for this one Feature.
- `feature show <ref> [--on <machine>]`
  Prints the full Feature state: the record, each child's state and its digest.
- `feature list`
  Prints every Feature in the folder.
- `feature sync <ref>`
  Merges the base branch into the feature branch early, to keep the final merge small.
- `feature cancel <ref>`
  Stops running children, removes queued ones, keeps the feature branch, deletes nothing.
- `feature merge <ref>`
  The person's Merge feature action. Only in phase `ready`. Merges the open pull request into the base (remote mode) or squash-merges locally (local mode), then moves the Feature to `done`.
- `feature fix-round <ref>`
  The person's Add a fix round action. Only when the Feature shows Needs you after the allowed fix rounds. Adds one more fix round and a new Verify Task.

Top-level commands:
- `fanout <change> [--dry-run] [--machines a,b,c] [--amend] [--no-start]`
  Turns an approved work table into child Tasks (C7 `fanout`). `--dry-run` creates nothing and prints the plan. `--machines` places packages across machines with chain affinity. `--amend` reads an updated table and adds only new rows (and cancels unstarted children of removed rows). `--no-start` creates the Tasks but does not start them.
- `sync-branch [--base <feature-branch>]`
  A worker runs it in its own worktree before `ready` and when the feature branch moves (C7 `branchsync`). `--base` overrides the feature branch to rebase onto (default: the Task's base).
- `feature-status <ref> [--on <machine>]`
  Returns the states and digests of the children of a Feature on one machine (C7 `feature_status`).
- `verify-report (--pass | --findings-file <file>) [--feature <ref>]`
  A Verify Task reports its result (C5 findings file, C6 exit codes). `--feature` names the Feature when the folder has more than one; otherwise it is the Feature the running Verify Task belongs to.
- `retro-data <ref>`
  Prints the retro JSON for a Feature (C5).
- `retro <ref>`
  Starts a retro for a Feature now, with the retro agent settings.
- `flow init [--update]`
  Prepares a repo (`openspec init`, the skill check, the pinned copies). `--update` moves the pinned skill copies to a newer commit.

New flags on the existing `add`:
- `--feature <ref>`: the Feature this Task belongs to (sets `feature`, and `role=child` when no `--role` is given).
- `--role spec|child|verify|fix|integration|retro`: the Task's flow role (one of the Task roles of C2). Requires `--feature`. `cmd_add` validates the value. The Assistant uses `--role fix` for a Fix Task, `--role verify` for a Verify Task and `--role integration` for an Integration Task.
- `--owns <glob[,glob...]>`: the owned paths. Repeatable; values add up.
- `--risk low|high`: the Task's risk (default `low`).

New filter on the existing `list`:
- `list --feature <ref>`: only the Tasks of that Feature, shown as its tree (`HAT-12 > HAT-14`).

### C5. JSON shapes

These are the shapes that cross a module or a machine boundary.
Keys are stable.

`fanout --dry-run` (and `fanout --json` prints the same with `"created"` counts added):

```json
{
  "change": "add-login-flow",
  "feature": "HAT-12",
  "valid": true,
  "waves": [["0.1"], ["1.1", "1.2"], ["2.1"]],
  "packages": [
    {
      "package": "1.1",
      "rows": ["1.1"],
      "wave": 1,
      "owns": ["src/parser/types.ts"],
      "depends": ["0.1"],
      "risk": "low",
      "risk_reason": "",
      "size": "S",
      "machine": "vm-a",
      "branch": "task/HAT-12-HAT-14-types",
      "base": "feature/HAT-12-login-flow",
      "after": ["HAT-13"],
      "merge": "checks",
      "brief": "the generated child brief"
    }
  ],
  "verify_task": { "after": ["HAT-14", "HAT-15"] },
  "recreated": [],
  "warnings": []
}
```

- `risk_reason` is `""` for a low-risk row; for a high-risk row it is `risk column is high` or `owns matches risky path <glob>`.
- `recreated` lists the packages whose child was removed by hand (`herdr-task rm`) and that this run re-creates, each `{ "package", "rows", "ref" }` in package order. It is empty on a first run and on an ordinary rerun. A brand-new row is an ordinary addition (in `packages`), not a re-creation; a row recorded skipped is a kept gap (proposal 6.12) and is never re-created, so it is never here.
- When the table is invalid, the shape is `{"change": "...", "valid": false, "code": "<code>", "error": "<message>"}`, with `code` one of `missing-id`, `unknown-dep`, `cycle`, `empty-owns`, `size-l`, `bad-risk`, `bad-table` (C6).

`feature-status`:

```json
{
  "feature": "HAT-12",
  "machine": "vm-b",
  "reachable": true,
  "children": [
    { "ref": "HAT-15", "state": "running", "role": "child", "rows": ["2.2"], "digest": null },
    { "ref": "HAT-16", "state": "done", "role": "child", "rows": ["2.3"], "digest": { "...": "see below" } }
  ]
}
```

- `reachable` is `false` and `children` is `[]` when the machine cannot be reached (the home machine goes on with the others).

The digest (one child's, from `digest.build`; stored under `feature.digests[digest.child_key(child)]`, C2, and returned by `feature-status` and `retro-data`):

```json
{
  "ref": "HAT-15",
  "role": "child",
  "feature": 12,
  "rows": ["2.2"],
  "state": "merged",
  "machine": "vm-b",
  "review": { "blocker": 0, "major": 1, "minor": 2 },
  "rounds": 1,
  "events": { "needs_you": 0, "stalled": 0, "failed": 0 },
  "rebase_conflicts": 0,
  "time": { "estimate_min": 30, "actual_min": 42 },
  "top_tools": [ { "tool": "Bash", "calls": 12, "ms": 34000 } ],
  "transcript": "/abs/path/to/transcript.jsonl"
}
```

- `state` is `merged`, `skipped`, `cancelled` or `failed`.
- `review` counts the findings of the last recorded round by level. `rounds` is the review rounds used.
- `time.estimate_min` is `null` when the Task gave no estimate.
- `top_tools` is ordered by `ms`, longest first, at most five entries.

`retro-data`:

```json
{
  "feature": { "ref": "HAT-12", "title": "Login flow", "change": "add-login-flow" },
  "digests": [ { "...": "each child's digest" } ],
  "checks": { "script": "bin/sync-shared --check", "last": { "ok": true, "ran_at": "2026-11-07T11:00:00Z" } },
  "ci": { "workflow": ".github/workflows/check.yml", "last": { "conclusion": "success", "url": "..." } }
}
```

- `digests` is the list of all children's digests of the Feature (empty list for a Feature with no children, which is still a valid result).
- `checks` and `ci` are `null` when the repo has no check script or no CI.

The findings file for `verify-report --findings-file <file>` is a JSON list:

```json
[
  { "level": "blocker", "title": "Auth bypass", "detail": "...", "paths": ["src/auth.ts"], "row": "2.2" },
  { "level": "major",   "title": "...",         "detail": "...", "paths": [],               "row": null }
]
```

- `level` is `blocker`, `major` or `minor`. `paths` and `row` may be empty or `null`.

#### Command success envelopes

The remaining commands print one JSON object with `--json`.
A refusal prints `{"error": "...", "reason": "<code>"}` instead (C6).

| Command | Success envelope |
|---|---|
| `feature add` | the Feature record (C2), enriched with `ref`. |
| `feature show` | `{ "feature": <record>, "ref": "HAT-12", "children": [ { "ref", "state", "role", "rows", "risk", "machine", "digest" } ], "findings": [ <finding> ] }` (the same as C7 `feature.show`). |
| `feature list` | `{ "features": [ <record with ref>, ... ] }`. |
| `feature sync` | `{ "ok": true, "merged": <bool>, "conflict": <bool> }`. |
| `feature cancel` | `{ "ok": true, "stopped": ["HAT-15"], "removed": ["HAT-16"] }`. |
| `feature merge` | `{ "ok": true, "pr": "<url or null>", "phase": "done" }`. |
| `feature fix-round` | `{ "ok": true, "fix_rounds": <n>, "verify_task": "HAT-32" }`. |
| `fanout` (real run) | the `fanout --dry-run` object plus `"created": { "children": <n>, "verify": 0 or 1 }` and `"started": <n>`. |
| `verify-report` | `{ "ok": true, "findings": <n>, "blockers": <n> }`. |
| `retro-data` | the retro-data object above. |
| `retro` | `{ "ok": true, "task": "<ref or null>", "mode": "off|ask|automatic", "saved_to": "outside|repo|pr" }`. |
| `flow init` | `{ "ok": true, "openspec": "initialized|present", "skills": { "<name>": "repo|user|added|skipped" }, "pr": "<branch or null>" }`. |
| `sync-branch` | the object in C6. |
| `feature-status` | the `feature-status` object above. |

### C6. Exit codes and messages

Every flow command returns `0` on success and `1` on an unexpected error or bad usage (a raised `Fail`, as today).
The guard and conflict outcomes below use a command-specific non-zero code, and, with `--json`, always print a `reason` code that is the real contract (callers branch on `reason`, not on the text).
Each module defines its codes as named constants.

`ready` (the guards in `ready_guards` and the plan check in `fanout`, called from `commands.cmd_ready`):

| Code | reason | When | Message |
|---|---|---|---|
| 0 | - | Accepted | (the normal ready output) |
| 2 | `owned-paths` | The diff changes paths outside `owns` | `ready refused: these changed paths are outside your owned paths: <paths>. Ask the Assistant to extend them.` |
| 3 | `plan-invalid` | A spec Task whose `openspec validate` or `fanout --dry-run` failed | `ready refused: the plan is not valid yet: <detail>.` |
| 4 | `review-stale` | No recorded review round matches the final commit's hash or patch-id | `ready refused: the last commit has no matching review round. Get a fresh review.` |

The owned-paths check exempts a built-in list of lockfiles and generated files, plus `tasks.owns_exempt`.
Built-in `ready_guards.DEFAULT_EXEMPT`: `bun.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `web/generated/**`, `**/*.generated.*`.

`sync-branch` (`branchsync`, from `commands`/`cli`):

| Code | reason | When | Message |
|---|---|---|---|
| 0 | `clean` | Already current, or a clean rebase (review stays valid when the patch-id is unchanged) | `sync-branch: clean` |
| 2 | `owned-paths` | The worker's own diff against the base changes paths outside `owns`; nothing is rebased (the same precheck `ready` uses, proposal section 6.8) | `sync-branch refused: these changed paths are outside your owned paths: <paths>. Ask the Assistant to extend them.` |
| 3 | `conflict` | The rebase conflicts; it is aborted and the branch is restored from the backup ref | `sync-branch: conflict, branch restored. Conflicted: <files>. Outside your owned paths: <files>. Regenerate: <lockfiles>.` |
| 4 | `checks-failed` | A clean rebase, but the post-rebase checks failed | `sync-branch: rebased, but checks failed: <detail>.` |

With `--json`, `sync-branch` prints `{"reason": "...", "conflicted": [...], "outside_owns": [...], "regenerate": [...], "backup_ref": "backup/<branch>-<time>"}`.

`verify-report` (`commands`):
- `0`: the report is recorded (either `--pass` or `--findings-file`). With `--json` it prints `{"ok": true, "findings": <n>, "blockers": <n>}`.
- `1`: an error (no running Verify Task or Feature, or a findings file that is not a valid list).

`fanout` and `fanout --dry-run`:
- `0`: the plan is valid (dry-run prints it; a real run creates and starts the children).
- `1`: the table is invalid; with `--json` it prints `{"valid": false, "code": "<code>", "error": "..."}` (the codes are in C5).

### C7. Cross-module Python API

For each new module, the public functions with their signatures, and the one entry function per CLI command that `cli.py` dispatches to.
Signatures use the existing style (`Store` instances, plain dicts for records, `Fail` for a hard error).
A function that shells out to git uses a real throwaway repo in its test; only `hc` and `gh` are mocked.

`worktable.py` (T05a, pure, no git):
- `parse_table(text: str) -> list[dict]` - the rows, each `{"id", "title", "owns": [...], "depends": [...], "risk", "size"}`. Raises `WorkTableError(code, message)`.
- `check_table(rows: list[dict]) -> None` - raises `WorkTableError` for a missing id, an unknown dependency, a cycle, an empty `owns`, a size `L` row or a bad `risk`/`size`. Codes as in C5/C6.
- `add_overlap_links(rows: list[dict]) -> list[dict]` - adds a dependency between two rows whose `owns` overlap and that have none; keeps table order; returns the rows.
- `waves(rows: list[dict]) -> list[list[str]]` - row ids grouped by dependency depth, by topological sort.
- `order(rows: list[dict]) -> list[str]` - the deterministic run order: wave first, then the longest chain of dependents, then larger size, then table order.
- `checkbox_ids(tasks_md: str) -> list[str]` - the ids from the `- [ ] <id> ...` lines.
- `tick(tasks_md: str, row_id: str) -> str` - returns the text with that row's box ticked.
- `WorkTableError(Exception)` with `.code` and `.message`.

`fanout.py` (T05b, depends on `worktable`, `store`):
- `plan(s: Store, change: str, machines: list[str] | None = None, amend: bool = False) -> dict` - the dry-run plan (C5). Does not create anything.
- `dry_run_json(plan: dict) -> dict` - the plan shape for `--json` (identity-like; present for symmetry and version stability).
- `run(s: Store, change: str, machines=None, amend=False, start=True) -> dict` - creates child Tasks (via `commands.cmd_add`/`store.new`) in order, adds the Verify Task, optionally starts them; returns the plan with `"created"` counts. A second run adds only missing children.
- `check_plan(folder: str, change: str) -> dict` - runs `openspec validate --json` and `plan`; returns `{"ok": bool, "detail": str}` for the `ready` plan guard.
- `cmd_fanout(args)` - the `fanout` command entry.

`phases.py` (T11a, pure rules, no git, no store writes):
- `phase_of(feature: dict, children: list[dict]) -> tuple[str, str]` - the `(phase, attention)` a Feature should have, given its children's states.
- `should_retry(task: dict) -> bool` - true when a failed or stalled Task may restart once (`retries < 1`).
- `fix_round_allowed(feature: dict, verify_rounds: int) -> bool` - true when another fix round is within the setting.
- `is_routine(event: str) -> bool` - true for `started`, `ready`, `merged` (not sent one by one); false for the exception events (C8).
- Constants: `PHASES` (the seven), `ATTENTION` (the reason codes of C2), `ROUTINE_EVENTS`, `EXCEPTION_EVENTS`.

`feature.py` (T11, depends on `settings`, `store`, `phases`):
- `detect_mode(folder: str) -> str` - `"remote"` when there is a GitHub remote and `gh` access, else `"local"`.
- `create(s: Store, title: str, base: str | None = None, change: str | None = None, mode: str | None = None, auto_fanout: bool = True) -> dict` - creates the Feature record, the feature branch and the spec Task; returns the Feature.
- `get(s: Store, ref) -> dict` - the Feature by ref; raises `Fail` if it is a Task or missing.
- `show(s: Store, feature: dict) -> dict` - the full state (record plus children states and digests).
- `sync(s: Store, feature: dict) -> dict` - merges the base into the feature branch early.
- `cancel(s: Store, feature: dict) -> dict` - stops running children, removes queued ones, keeps the branch.
- `merge(s: Store, feature: dict) -> dict` - the Merge feature action (phase `ready` only).
- `fix_round(s: Store, feature: dict) -> dict` - the Add a fix round action.
- `on_spec_accepted(s: Store, task: dict) -> None` - the Plan approval hook: records Plan approval and moves the Feature to `building`. It does not fan out directly, because Accept can return while the spec Task's merge is still pending (GitHub checks) or only requested (local). The fan-out trigger is in `tick.py` and waits for the spec merge to land on the feature branch (C8). Called from `commands.cmd_accept` after the normal accept.
- `spec_merged(s: Store, feature: dict) -> bool` - true when the spec Task has merged and the approved work table is on the feature branch; the fan-out trigger reads it.
- `slug_title(title: str) -> str` and `change_name(title: str) -> str` - the branch short-title slug and the change name.
- `cmd_feature(args)` - the `feature` command entry; dispatches on `args.feature_cmd` (`add`, `show`, `list`, `sync`, `cancel`, `merge`, `fix-round`).

`closing.py` (T16, depends on `store`, `worktable`, `feature`; tick-driven, no CLI entry):
- `close(s: Store, feature: dict) -> dict` - runs the closing steps in order and returns the result.
- `tick_merged_rows(s: Store, feature: dict) -> list[str]` - ticks every row whose child merged; one commit on the feature branch; returns rows with no merged child (which set `attention = "row-no-child"`).
- `archive_change(folder: str, change: str) -> None` - runs `openspec archive <change> -y`, commits on the feature branch.
- `merge_base_in(folder: str, feature: dict) -> dict` - merges the base into the feature branch; on a conflict returns `{"conflict": True}` so the caller aborts and makes an Integration Task.
- `open_pr(s: Store, feature: dict) -> dict` - opens the pull request into the base; moves the Feature to `ready`.
- `make_integration_task(s: Store, feature: dict) -> dict` - creates the risky Integration Task (base = the feature branch).

`feature_status.py` (T17, depends on `store`, `digest`):
- `collect(s: Store, feature: dict) -> dict` - the states and digests of the children on this machine (the `feature-status` shape of C5).
- `gather(s: Store, feature: dict, machines: list[str]) -> dict` - one call per machine from the home machine; merges the results; an unreachable machine does not stop the others.
- `cmd_feature_status(args)` - the `feature-status` command entry.

`digest.py` (T08, depends on `store`, the JS `core/chat-log.js` for the log):
- `build(s: Store, task: dict) -> dict` - the digest (C5) from a Task and its replayed log.
- `child_key(child: dict) -> str` - the machine-qualified key for `feature.digests` (C2): `"<machine>:<id>"`, with `"local"` for the home machine.
- `DIGEST_FIELDS` - the field list, for the tests.
- No CLI entry; it is written when a child ends (C8).

`retro_data.py` (T08, depends on `store`, `digest`):
- `build(s: Store, feature: dict) -> dict` - the retro JSON (C5) from the Feature's digests plus the check-script and CI state.
- `cmd_retro_data(args)` - the `retro-data` command entry.

`retro.py` (T18, depends on `settings`, `store`, `retro_data`):
- `retro_agent(s: Store, space=None) -> dict` - the retro agent `{agent, model, effort}`, the Reviewer's when `retro.*` is empty.
- `start(s: Store, feature: dict, mode: str | None = None) -> dict | None` - creates the retro Task with the retro agent and the `retro_save` target; returns the Task, or `None` when `tasks.retro` is `off` and `mode` was not forced.
- `cmd_retro(args)` - the `retro` command entry.

`ready_guards.py` (T06, depends on git in the folder):
- `changed_paths(folder: str, base: str) -> list[str]` - the names the diff against `base` changes.
- `owned_paths_check(changed: list[str], owns: list[str], exempt: list[str] | None = None) -> dict` - `{"ok": bool, "outside": [...]}`; a path that matches `owns` or the exempt list is inside.
- `patch_id(folder: str, base: str) -> str` - the git patch-id of the whole diff against `base` (empty string for an empty diff).
- `review_valid(task: dict, commit: str, patch: str) -> bool` - true when a recorded round has the same commit hash or the same patch-id.
- Constants: `DEFAULT_EXEMPT` (C6), and the reason/exit codes `OWNED_PATHS = 2`, `REVIEW_STALE = 4`.

`branchsync.py` (T07, depends on git in the worktree, reuses `ready_guards`):
- `sync(folder: str, feature_branch: str, owns: list[str], mode: str, checks: bool = True) -> dict` - first runs the owned-paths precheck (via `ready_guards.owned_paths_check` on the diff against the base, proposal section 6.8) and refuses with `owned-paths` before touching the branch; then does rules 4 to 8 of proposal section 6.9. Returns `{"reason", "outside_owns", "conflicted", "regenerate", "backup_ref"}` (C6); `outside_owns` is the precheck's list on an `owned-paths` refusal, and the conflict files outside `owns` on a `conflict`.
- `backup_ref(folder: str, branch: str) -> str`, `enable_rerere(folder: str) -> None`, `post_checks(folder: str, paths: list[str]) -> dict` - helpers.
- `cmd_sync_branch(args)` - the `sync-branch` command entry.
- Constants: the reason/exit codes `CLEAN = 0`, `OWNED_PATHS = 2`, `CONFLICT = 3`, `CHECKS_FAILED = 4`.

`flow_init.py` (T10, depends on git and the filesystem; `hc` mocked in tests):
- `init(folder: str, update: bool = False) -> dict` - runs `openspec init`, checks the skills, copies the missing ones after a yes, commits a pull request; returns a report.
- `skill_present(name: str, folder: str, machine: str | None = None) -> str | None` - `"repo"` (`.claude/skills`), `"user"` (`~/.claude/skills`) or `None`.
- `copy_skill(name: str, folder: str, commit: str) -> None` - copies a skill pinned to a source commit, keeping the MIT notice.
- `cmd_flow_init(args)` - the `flow init` command entry.
- Constants: `SKILLS = ["grill-with-docs", "grilling", "domain-modeling", "to-spec", "retro", "writing-for-agents"]`, `MATT_POCOCK_REPO`, `MATT_POCOCK_COMMIT`.

### C8. Hooks that commands.py and tick.py call

`commands.py` (T20b) calls the flow modules at these points, and does nothing new for a classic Task:
- `cmd_ready`: for a Task with `owns`, run `ready_guards.changed_paths` then `ready_guards.owned_paths_check`; refuse with code 2 on a path outside. For a `role="spec"` Task, run `fanout.check_plan`; refuse with code 3 when it is not valid. For the review check, read the final commit's `ready_guards.patch_id` and pass it to `ready_guards.review_valid`; refuse with code 4 when stale.
- `cmd_reviewed`: store `ready_guards.patch_id(folder, base)` on the recorded round, next to the commit hash.
- `cmd_accept`: when the accepted Task is a spec Task (`role="spec"` with a `feature`), call `feature.on_spec_accepted` (the Plan approval hook) after the normal accept.
- `cmd_add`: `--feature`, `--role`, `--owns`, `--risk` set the Task fields of C2.
- `cmd_list`: `--feature` filters and groups.
- `cmd_verify_report`: the `verify-report` command entry (it lives in `commands.py`). It finds the running Verify Task (or the one of `--feature`), stores `verify_pass` and, for `--findings-file`, the `findings` list on that Task (C2), and prints `{"ok": true, "findings": <n>, "blockers": <n>}`. The next tick reads the Verify Task's result through `phases.phase_of`.

`tick.py` (T19) calls, each tick, for every Feature in a folder:
- `feature_status.gather` to bring children's states and digests to the home machine (remote mode, several machines).
- `phases.phase_of` to decide the phase and attention, and `feature.*` to write the new phase.
- `fanout.run` as the fan-out trigger: once a Feature is in `building`, its `auto_fanout` is `true`, and `feature.spec_merged` is true (the approved work table is on the feature branch). It runs idempotently (a second run adds only missing children), so a deferred spec merge simply delays the fan-out to a later tick, and a tick that fires twice creates no duplicates.
- `phases.should_retry` and the existing resume path to restart a failed or stalled child once; then set `attention`.
- `digest.build` when a child ends, writing it to `feature.digests`.
- `closing.close` when a Feature is in `closing`.
- `retro.start` when a Feature becomes `done` and `tasks.retro` is `automatic`.
- the news module to send the Assistant the wave and phase-change summaries, and the one-by-one exception events (`phases.EXCEPTION_EVENTS`): `needs-you`, `failed`, `stalled`, `needs-plan`, `needs-owns`, a Risky Task waiting, and verify findings.

### C9. The JS side

A new shared core module, `core/features.js` (T21a), source in `core/`, copied by `bin/sync-shared` (add it to `lib/COPIES` for the mods that use it: `tasks`, `jump`, `chat`, and the service and the web app load it from `core/`):

`core/features.js` consumes records that already carry their `ref`.
The service model enriches every Feature record with `ref` (computed from the folder prefix, the same way task rows get their ref), and with `folder` and `machine`, before it publishes them; each task row already carries its own `ref` and its `feature` id.
So `core/features.js` never computes a ref and never needs the prefix.

```js
// Group a folder's Tasks under their Feature.
// features: ref-enriched Feature records; tasks: ref-enriched task rows (each with a feature id or null).
// Returns [{ feature, tasks: [...], row }], and loose tasks under feature: null.
export function groupByFeature(features, tasks)

// The one summary row for a Feature group.
// feature: a ref-enriched Feature record; children: its ref-enriched task rows.
// Returns { ref, title, phase, phaseLabel, merged, total, findings, actions, attention }.
export function featureRow(feature, children)

export const PHASE_LABEL = {
  planning: "Planning", building: "Building", verifying: "Verifying",
  closing: "Closing", ready: "Ready to merge", done: "Done", cancelled: "Cancelled",
};

// The two human actions, and when each is offered.
export const FEATURE_ACTIONS = {
  merge: { id: "feature-merge", label: "Merge feature" },
  fixRound: { id: "feature-fix-round", label: "Add a fix round" },
};
export function mergeReady(feature)      // true in phase "ready"
export function canAddFixRound(feature)  // true when attention is "verify-open"
```

- `featureRow(...).merged` is the count of children merged, `total` the child count, `findings` the open verify findings count.
- The row offers `FEATURE_ACTIONS.merge` when `mergeReady`, and `FEATURE_ACTIONS.fixRound` when `canAddFixRound`.

The service snapshot (T21c, `service/model.ts`):
- The model reads each folder's `features` list (the list JSON already carries it, since the store file holds it) and publishes a `features` array beside `tasks`.
- The web client's snapshot gains a `features` field: `{ type: "snapshot", snapshot: { places, machines, tasks, features, ... } }`.
- Each task row already carries its `feature` id (C2); the web and the window group by it with `groupByFeature`.
- A `features` entry is the Feature record of C2, enriched with `ref`, `folder` and `machine`, plus the computed `featureRow` summary fields.

The service actions (T21c, `service/actions.ts`), each dispatched on `b.type`:
- `{ type: "feature-merge", feature: "<ref>", folder, machine }` runs `herdr-task feature merge <ref>` (the Merge feature action).
- `{ type: "feature-fix-round", feature: "<ref>", folder, machine }` runs `herdr-task feature fix-round <ref>` (the Add a fix round action).
- Both return `{ ok: true }` on success and throw the command's error otherwise, exactly as the existing `accept` action does.

### C10. Fixtures

The shared fixtures live in `test/fixtures/openspec-flow/`.
Tasks on both the Python and the JS side read them.
They are valid JSON (except the Markdown tables) and match the shapes above.

- `store.json` - a store file with a `features` list and child Tasks in several phases (planning, building, verifying), to test grouping, phase reads and the old-code-ignores-features rule.
- `snapshot.json` - a web snapshot with `features`, to test `groupByFeature` and the Feature row.
- `tasks-valid.md` - a `tasks.md` with a valid work table and its checkbox lines.
- `tasks-invalid-cycle.md`, `tasks-invalid-unknown-dep.md`, `tasks-invalid-empty-owns.md`, `tasks-invalid-size-l.md`, `tasks-invalid-bad-risk.md` - one invalid table each, for the `fanout`/`worktable` error codes of C5/C6.

### C11. The task plan

The keys are the proposal's ids (T00 to T25); the Shepherdr refs differ.
Each line: key, title, owned files, depends.
Tasks in one wave do not own the same file.
A module another task builds in parallel is not on your branch yet: call it only through the signatures above, and mock it in your tests.

| Key | Title | Owned files | Depends |
|---|---|---|---|
| T00 | Contract + proposal into docs | `docs/openspec-flow-proposal.md`, `docs/openspec-flow.md` (contract section), `test/fixtures/openspec-flow/*` | none |
| T01 | Python settings | `settings.py`, `assistant_settings.py`, `test/test_flow_settings.py` | T00 |
| T02 | Shared JS settings | `core/settings.js` (its `lib/` source and the copies by `bin/sync-shared`) | T00 |
| T03 | Flow + retro templates | `templates/flow-spec.md`, `flow-task.md`, `flow-verify.md`, `retro.md` | T00 |
| T04 | Store | `store.py`, `test/test_feature_store.py` | T00 |
| T05a | Work table | `worktable.py`, `test/test_worktable.py` | T00 |
| T06 | Ready guards | `ready_guards.py`, `test/test_ready_guards.py` | T00 |
| T07 | sync-branch | `branchsync.py`, `test/test_branchsync.py` | T00 |
| T08 | Digest + retro-data | `digest.py`, `retro_data.py`, `test/test_digest.py`, `test/test_retro_data.py` | T00 |
| T09 | Scale measurement | `test/test_scale.py` | T00 |
| T10 | flow init | `flow_init.py`, `test/test_flow_init.py` | T00 |
| T11a | Phases | `phases.py`, `test/test_phases.py` | T00 |
| T14 | Role template pointers | `templates/worker.md`, `reviewer.md`, `assistant.md`, `templates.py`, `test/test_flow_templates.py` | T00 |
| T20a | CLI skeleton | `cli.py`, `test/test_flow_cli.py` | T00 |
| T21a | Shared Feature grouping | `core/features.js`, `core/features.test.js`, `lib/COPIES` (and the copies) | T00 |
| T21c | Service snapshot + actions | `service/model.ts`, `service/actions.ts` (and their tests) | T00 |
| T23h | Fake hc/gh harness | `test/flowfakes.py`, `test/test_flowfakes.py` | T00 |
| T05b | fanout | `fanout.py`, `test/test_fanout.py` | T04, T05a |
| T11 | Feature commands | `feature.py`, `test/test_feature.py` | T01, T04, T11a |
| T12 | Window settings rows | `mods/shepherdr-settings/main.js`, `lib/coordinator.js` (and the copies) | T02 |
| T13 | Web settings rows | `service/settings.ts`, `web/src/Settings.tsx` | T02 |
| T15 | brief() injection | `launch.py`, `test/test_flow_brief.py` | T01, T03 |
| T16 | Closing | `closing.py`, `test/test_closing.py` | T04, T05a |
| T17 | feature-status | `feature_status.py`, `test/test_feature_status.py` | T04, T08 |
| T18 | retro command | `retro.py`, `test/test_retro.py` | T01, T03, T08 |
| T20b | commands.py hooks | `commands.py`, `test/test_flow_commands.py` | T04, T06, T20a |
| T21 | Web Feature row | `web/src/groups.ts`, `groups.test.ts`, `Panel.tsx` | T21a, T21c |
| T22 | Window Feature row | `mods/tasks/main.js` | T21a |
| T19 | Feature tick | `tick.py`, `test/test_feature_tick.py` | T05b, T08, T11, T11a, T16, T17, T20b |
| T24 | Final docs | `docs/openspec-flow.md` (not the contract or scale sections), `docs/settings.md`, `docs/develop.md` | T05b, T11, T15, T16, T18, T20b |
| T23r | E2E remote mode | `test/test_flow_e2e.py` | T19, T23h, T24 |
| T23l | E2E local mode | `test/test_flow_e2e_local.py` | T19, T23h, T24 |
| T25 | Scale ramp | `docs/openspec-flow.md` (the scale section) | T23r, T23l |

Python files without a path live in `mods/tasks/bin/herdr_task/`.

<!-- CONTRACT END -->

## Overview

The OpenSpec flow is an optional way to build one large change with many agents at once.
It is off by default.
The `tasks.flow` setting turns it on: `classic` (the default) keeps every Task exactly as it was, and `openspec` enables the Feature flow for a space or for one Task.
A space with `flow=classic` behaves exactly as before, byte for byte in every brief.

A Feature is one OpenSpec change that groups many Tasks under one feature branch.
It is a record of its own in the store of its folder, not a Task (ADR-0001).
A Feature draws its number from the same folder counter that Tasks use, so a ref such as `HAT-12` is a Feature or a Task, never both (ADR-0002).
Each Task that belongs to a Feature carries a `feature` field, and the CLI and both user interfaces group the Tasks under the Feature with it, for example `HAT-12 > HAT-14`.

The flow's aim is to keep the person's input at two points only: the start, when the plan is written, and the end, when the Feature merges.
In between, the Child Tasks build in parallel, each on its own branch, each reviewed by its worker's reviewer, each merging into the feature branch after checks.
The person also accepts each Risky Task and each Amendment.
A small request stays an ordinary Task, even in a space with `flow=openspec`: the flow starts only when someone creates a Feature.

Use the glossary of [the proposal](openspec-flow-proposal.md) (section 2) for every term here.
The names, fields, JSON shapes, exit codes and module signatures are fixed in the contract above.
This part of the file describes what the code does on main; the contract is the source of truth for every name.

### What is built so far

The building blocks are on main and are driven by the commands below.
The scheduler integration that drives a Feature from one phase to the next by itself is not merged yet, so today you run the steps with the commands rather than watch them happen on their own.

On main now:

- The Feature record, its phases and the flow Task fields in the store.
- The `feature`, `fanout`, `sync-branch`, `feature-status`, `verify-report`, `retro-data`, `retro` and `flow init` commands, and the new `add` flags.
- The `ready` guards (owned paths, the spec-Task plan check, and the patch-id review check) and the patch-id stored by `reviewed`.
- The flow files added to a flow Task's brief by `brief()`.
- The grouped Feature row in the window and the web app, with its Merge feature and Add a fix round actions.
- Accept of a spec Task moves its Feature from `planning` to `building` by itself.

Not wired yet (the scheduler tick that the contract C8 describes is a later task):

- Automatic fan-out on Plan approval. For now you run `herdr-task fanout <change>` yourself.
- Automatic phase changes past `building`, the closing steps, the failed-child retry, the written digests and the Assistant news. The closing steps have no command of their own yet, so a Feature does not reach `ready` on its own.
- Automatic retro. For now you run `herdr-task retro <ref>` yourself.
- Fan-out across machines. A real run creates children on the home machine only (see The commands).

## The lifecycle and its phases

A Feature moves through seven phases.
The `phase` field of the Feature record holds the current one.
The table gives the meaning of each phase; the steps below say which moves run by themselves on main and which you run with a command.

| Phase | Meaning |
|---|---|
| `planning` | The spec Task exists and is not accepted. A worker writes one OpenSpec change with the person and adds the work table to `tasks.md`. |
| `building` | Plan approval is done. The Child Tasks build in parallel. The Feature has no agent of its own. |
| `verifying` | Every child is merged or skipped. A Verify Task runs `/opsx:verify` on the feature branch, read-only. |
| `closing` | Verify is clean. The closing steps tick the rows, archive the change, merge the base in, and open the pull request. |
| `ready` | The pull request is open. The person presses Merge feature. |
| `done` | The pull request is merged. A retro runs if the setting asks for it. |
| `cancelled` | The person cancelled the Feature. It keeps the feature branch and deletes nothing. |

The steps of a Feature, and how each one works on main:

1. Start.
   The person asks for a Feature, or the Assistant proposes one and the person says yes.
   `herdr-task feature add "<title>"` creates the Feature, the feature branch and the spec Task (phase `planning`).
2. Plan.
   The spec worker picks path A or path B (see How to try the flow) and writes the change.
   `ready` is refused until `openspec validate` and `fanout --dry-run` both pass.
   The person's Accept is Plan approval, which moves the Feature to `building` by itself.
3. Fan-out.
   Run `herdr-task fanout <change>` to turn the approved work table into the Child Tasks and start them.
   The automatic trigger on Plan approval is not wired yet, so this step is a command for now.
4. Build.
   The children work in parallel.
   Each merges into the feature branch after its checks.
   A Risky Task waits for the person's Accept first.
5. Verify.
   Add a Verify Task that runs `/opsx:verify` on the feature branch, and it reports with `herdr-task verify-report`.
   Findings become Fix Tasks.
   The Feature allows a number of fix rounds set by `tasks.verify_rounds` (two by default).
6. Closing.
   The closing steps tick the rows, archive the change, merge the base in, and open the pull request (phase `ready`).
   These steps are tick-driven in the contract and are not reachable by a command yet.
7. Merge and retro.
   The person presses Merge feature, or runs `herdr-task feature merge <ref>`, which moves the Feature to `done`.
   Run `herdr-task retro <ref>` for the retro; the automatic retro on `done` is not wired yet.

A Feature has exactly two human actions of its own:

- Merge feature, in phase `ready`: `herdr-task feature merge <ref>`.
- Add a fix round, when the Feature shows Needs you after the allowed fix rounds: `herdr-task feature fix-round <ref>`.

Accept on a spec Task, a Risky Task or an Integration Task keeps its normal meaning: merge that Task's work.

## The commands

Every command takes `--folder <path>` (default: here) and `--json`, like the existing commands.
With `--json` a command prints one JSON object; the shapes are in the contract (C5).

### Feature

`herdr-task feature <subcommand>` manages one Feature.

| Subcommand | What it does |
|---|---|
| `feature add "<title>"` | Creates the Feature, the feature branch and the spec Task. Flags: `--base <branch>` (default: the branch checked out in the folder, normally main), `--change <name>` (default: generated from the title), `--mode remote\|local` (default: detected), `--no-auto-fanout` (reserved for the automatic fan-out trigger, which is not wired yet). In local mode it prints `No GitHub remote: local mode, one machine only` first. |
| `feature show <ref>` | The full Feature state: the record, each child's state and its digest. `--on <machine>` reads a Feature on another machine. |
| `feature list` | Every Feature in the folder. |
| `feature sync <ref>` | Merges the base branch into the feature branch early, to keep the final merge small. |
| `feature cancel <ref>` | Stops running children and removes queued ones. It keeps the feature branch and deletes nothing. |
| `feature merge <ref>` | The person's Merge feature action, in phase `ready` only. Merges the pull request into the base (remote mode) or squash-merges locally (local mode), then moves the Feature to `done`. |
| `feature fix-round <ref>` | The person's Add a fix round action. Adds one more fix round and a new Verify Task. |

### Top-level flow commands

| Command | What it does |
|---|---|
| `fanout <change>` | Turns an approved work table into Child Tasks and starts them. `--dry-run` creates nothing and prints the plan. `--machines a,b,c` previews a spread across machines with chain affinity in `--dry-run`; a real run creates children on the home machine only (cross-machine creation is a planned follow-up, so a real `--machines` run is refused). `--amend` reads an updated table and adds only new rows (and cancels unstarted children of removed rows). `--no-start` creates the Tasks but does not start them. A second run adds only missing children, so it is safe to run again. |
| `sync-branch` | A worker runs it in its own worktree before `ready` and when the feature branch moves. It rebases the Task branch onto the feature branch with a backup ref and `rerere`, and refuses before touching the branch if the diff goes outside the owned paths. `--base <feature-branch>` overrides the branch to rebase onto. |
| `feature-status <ref>` | The states and digests of a Feature's children on one machine. `--on <machine>` reads another machine. The home machine calls it once per machine each tick. |
| `verify-report (--pass \| --findings-file <file>)` | A Verify Task reports its result. `--feature <ref>` names the Feature when the folder has more than one. |
| `retro-data <ref>` | The retro JSON for a Feature: every child's digest, plus the check-script and CI state. |
| `retro <ref>` | Starts a retro for a Feature now, with the retro agent settings. |
| `flow init` | Prepares a repo: it runs `openspec init`, checks for each Matt Pocock skill at repo level (`.claude/skills`) and user level (`~/.claude/skills`), asks before it copies a missing one (pinned to a source commit, MIT notice kept), and commits the result on its own branch (`chore/flow-init`). It makes a local branch and commit for you to review; it does not push or open the pull request itself. `--update` moves the pinned skill copies to a newer commit on `chore/flow-init-update`. |

### New flags on `add` and `list`

`herdr-task add` gains four flags for a Child Task:

- `--feature <ref>`: the Feature this Task belongs to. It sets the Task's role to `child` unless `--role` is given.
- `--role spec|child|verify|fix|integration|retro`: the Task's flow role. It requires `--feature`.
- `--owns <glob[,glob...]>`: the owned paths. It is repeatable and the values add up.
- `--risk low|high`: the Task's risk (default `low`). A `high` Task gets `merge=ask`.

`herdr-task list --feature <ref>` shows only that Feature's Tasks, as its tree.

### The flow role in a brief

When the Settings block says `Flow: openspec` and a Task has a flow role, `brief()` adds one flow file after the normal worker words.
The spec Task gets `flow-spec`, a Child, Fix or Integration Task gets `flow-task`, and the Verify Task gets `flow-verify`.
A classic Task, or an ordinary Task in an `openspec` space with no flow role, gets none, so its brief stays byte for byte the same as before.
A flow Task in a repo with no `openspec/` folder stops with a clear message that says to run `herdr-task flow init` first.

### Owned paths and review validity

A Child Task changes only its owned paths.
`herdr-task ready` compares the Task's diff against the base, and refuses if a changed path is outside the `owns` list (exit code 2, reason `owned-paths`).
A built-in exempt list of lockfiles and generated files is allowed, and `tasks.owns_exempt` adds to it.
If a worker needs a file that is not theirs, they set the status line to "needs owns change" or "needs plan change", ask the Assistant, and wait; the Assistant can extend the owned paths.

A review round is recorded with the commit hash and the git patch-id of the whole diff against the base.
A clean rebase keeps the diff the same, so the patch-id is unchanged and the review stays valid.
Conflict resolution, a regenerated file or a new commit changes the patch-id, and the worker then needs a fresh review round (`ready` refuses with reason `review-stale`).

## The two modes

A Feature runs in `remote` mode or `local` mode.
The mode is detected when the Feature is created and fixed from then on.

| | Remote mode | Local mode |
|---|---|---|
| Condition | A GitHub remote and `gh` access | No GitHub remote |
| Child merge | A pull request, squash-merged into the feature branch | Fast-forward into the feature branch, under a repo lock |
| Feature merge | A pull request, squash-merged into the base branch | A squash merge into the base branch, as the classic local merge does |
| Machines | Any number | One machine only |
| At creation | Normal | `feature add` prints `No GitHub remote: local mode, one machine only` |

Local mode works on one machine only.
It refuses to place a Child Task on another machine.
Remote mode is the one that allows more than one machine, because each child fetches the feature branch from GitHub.
On main, though, a real `fanout` run creates children on the home machine only in either mode, so more than one machine is a planned follow-up, not yet live (see The commands).

## The settings

The flow adds these settings.
They work for a space and for everywhere, like the existing task settings, and default to off or to the safe value.
The contract (C1) has the exact keys, values and defaults; `docs/settings.md` says where to set them.

| Setting | Values (default) | What it does |
|---|---|---|
| Flow | classic, openspec (classic) | Turns the Feature flow on for a space or a Task. It does not turn every Task into a Feature. |
| Verify rounds | 1 to 5 (2) | How many fix rounds a Feature allows before it shows Needs you. |
| Risky paths | a list of globs (empty) | A row whose owned paths match one of these is high risk, so its child gets `merge=ask`. |
| Owned-paths exempt | a list of globs (empty) | Globs added to the built-in exempt list of the owned-paths guard. |
| Retro | off, ask me, automatic (off) | Whether a retro runs after a Feature is `done`. `ask me`: the Assistant asks first. `automatic`: it starts by itself. |
| Retro prompt | built-in, or a skill name, or a file path (built-in) | The retro instructions. |
| Retro saves to | outside the repo, in the repo (`docs/retros/`), pull request comment (outside the repo) | Where the retro report is saved. |
| May change without asking | nothing, AGENTS.md, specs and ADRs (nothing) | What a flow agent may change without the person's Accept. |
| Retro agent | a CLI, model and effort, like a role (the Reviewer's) | The agent that runs the retro. Empty means the Reviewer's agent, model and effort. |

Both user interfaces read the same `~/.herdr/shepherdr.json`.
Risky paths and the owned-paths exempt list are per-space list editors, not per-Task `--set` values.
The `automatic` value of Retro waits on the scheduler integration that is not wired yet; for now `herdr-task retro <ref>` starts a retro by hand whatever the setting says.

## How to try the flow

Try it on a small throwaway repo first, never on a repo whose main branch you cannot afford to break.
The scheduler does not yet drive the flow end to end, so the steps below run it with the commands.

1. Prepare the repo.
   Run `herdr-task flow init` in it.
   It runs `openspec init` and checks for the Matt Pocock skills, and it asks before it copies a missing one.
   It leaves a local branch (`chore/flow-init`) with the change committed, for you to review and push.
2. Turn the flow on.
   Set `flow=openspec` for the space, in Settings (the Tasks page).
   The space setting is what reaches the spec Task and every child; a per-Task `--set flow=openspec` on the spec Task alone does not reach the children fan-out creates.
3. Create a Feature.
   Run `herdr-task feature add "<title>"`.
   It makes the Feature, the feature branch and the spec Task.
   In a repo with no GitHub remote it says so and uses local mode, one machine only.
4. Write the plan.
   The spec worker asks you path A or path B.
   Path A (`/opsx:explore`, then `/opsx:propose`) suits a change whose shape is clear.
   Path B (`/grill-with-docs`, then `/to-spec`, then `/opsx:propose`) grounds it first.
   Either way the change gets a work table in `tasks.md`.
   The worker cannot say `ready` until `openspec validate` and `fanout --dry-run` both pass.
5. Approve the plan.
   Accept the spec Task.
   That is Plan approval, and it moves the Feature to `building`.
   To see the plan first without creating anything, run `herdr-task fanout <change> --dry-run`.
6. Fan out.
   Run `herdr-task fanout <change>` to create the Child Tasks and start them.
   They work in parallel and merge into the feature branch after checks.
   A Risky Task waits for your Accept.
   Follow the Feature with `herdr-task feature show <ref>`, or the grouped Feature row in the window and the web app.
7. Verify.
   Add a Verify Task for the feature branch, which runs `/opsx:verify` and reports with `herdr-task verify-report`.
   Open findings become Fix Tasks, up to the allowed fix rounds.
8. Close and merge.
   The closing steps and the move to `ready` are tick-driven and not reachable by a command yet, so this last stretch is not runnable end to end on main.
   When it lands, you will press Merge feature (or run `herdr-task feature merge <ref>`), and run `herdr-task retro <ref>` for the retro.

To scale up, raise Tasks at once for the space, and ramp the number of parallel agents in steps (10, 25, then 50), measuring at each step.
See [docs/develop.md](develop.md) for how to run the checks.
