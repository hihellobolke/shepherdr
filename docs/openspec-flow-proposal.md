# Proposal: OpenSpec flow and retro for Shepherdr

Status: approved.
No code is written yet.
Writing style: about 80% ASD-STE100 (short sentences, active voice, present tense, one idea per sentence, fixed terms).
Sections 1 to 11 are the proposal.
Sections 12 and 13 are two decision records (ADR-0001 and ADR-0002).
Section 14 lists the points that stay open.

## 1. Purpose

Shepherdr must let the person give input at the start of a feature and at the end.
In between, many agents work in parallel.
This proposal adds an optional "OpenSpec flow" to Shepherdr.
The flow uses OpenSpec skills (`/opsx:*`) and Matt Pocock skills (`grill-with-docs`, `to-spec`, `retro`).
The goal is 40 to 50 agents at the same time, on one machine or on four or five machines.
The classic flow does not change.

## 2. Glossary

Use these terms in all new text, code and docs.
Do not use the words in the "Avoid" column.

### 2.1 Work items

| Term | Meaning | Avoid |
|---|---|---|
| Task | One unit of work for one worker. It has one branch and one pull request. Accept on a Task always means "merge its work". | Job, ticket |
| Feature | One OpenSpec change that groups many Tasks under one feature branch. It is a record of its own. It is not a kind of Task. | Feature task, epic, project |
| Ref | The short name of a Task or a Feature, for example `HAT-12`. Both draw their number from the same counter of the folder. | Id (for the display name) |
| Spec Task | The Task in which a worker writes the OpenSpec change with the person. A Feature has one first spec Task. | Planning task |
| Child Task | A Task that does one work package of a Feature. | Subtask, sub-task |
| Work package | One or more rows of the work table that one child Task does. | Chunk |
| Verify Task | A read-only Task that runs `/opsx:verify` on the feature branch. | Check task |
| Fix Task | A child Task that fixes a finding of a Verify Task. | Retry task |
| Integration Task | A risky child Task that resolves a conflict between the feature branch and its base branch. It does not count as a fix round. | Merge fix, rebase task |
| Amendment | A spec Task of an existing Feature that changes the approved plan. | Re-plan, scope change |
| Risky Task | A child Task that the person must accept before it merges. | Critical task, manual task |

### 2.2 Plan

| Term | Meaning | Avoid |
|---|---|---|
| Change | One folder in `openspec/changes/`. It holds the proposal, the design, the delta specs and `tasks.md`. | Spec folder |
| Work table | The table in `tasks.md` with one row for each unit of work. Section 6.5 gives its format. | Task list |
| Row | One line of the work table. A row has an id such as `2.1`. | Item |
| Owned paths | The files that a child Task may change. They come from the `owns` column of its rows. | Scope, ownership list |
| Risk | The `low` or `high` value in the work table. | Priority |
| Risky paths | A per-space list of globs. A row that touches one of them is high risk. | Protected paths |
| Plan approval | The person accepts a spec Task. The approved spec is then in the feature branch, and the Feature starts building. | Sign-off |
| Wave | A group of rows that have the same depth in the dependency graph. | Phase, stage |
| Chain | A line of rows where each row depends on the one before it. | Pipeline |
| Fan-out | The step that turns an approved work table into child Tasks. It runs by itself after Plan approval. | Split, breakdown |

### 2.3 Lifecycle

| Term | Meaning | Avoid |
|---|---|---|
| Phase | The state of a Feature: `planning`, `building`, `verifying`, `closing`, `ready`, `done` or `cancelled`. | Stage |
| Fix round | One batch of Fix Tasks, followed by a new Verify Task. A Feature allows two. | Retry, iteration |
| Closing | The deterministic steps after a clean verify: tick rows, archive, merge the base branch in, open the pull request. | Finalize |
| Needs you | A flag on a Feature or Task that says the person must decide. | Blocked |
| Digest | A short record that a child Task leaves when it ends. It holds the facts that the retro needs. | Summary |
| Retro | The review of the agent environment after a Feature merges. It follows Matt Pocock's `retro` skill. | Postmortem |

### 2.4 Git and machines

| Term | Meaning | Avoid |
|---|---|---|
| Feature branch | The branch `feature/<SLUG>-<NUM>-<short-title>`. Nobody rewrites it. | Integration branch |
| Task branch | The branch `task/<FEATURE-REF>-<CHILD-REF>-<short-title>`. One worker owns it. | Topic branch |
| Base branch | The branch that a Feature or Task merges into. Normally it is main. | Target branch |
| Remote mode | A Feature in a repo with a GitHub remote and `gh` access. The remote is the sync point. | Online mode |
| Local mode | A Feature in a repo with no GitHub remote. It works on one machine only. | Offline mode |
| Patch-id | The hash that git computes from the changed lines of a diff. It ignores commit hashes. | Diff hash |
| Home machine | The machine of the Assistant that started the Feature. The Feature record lives there. | Hub, primary |
| Chain affinity | The rule that a chain of rows runs on one machine. | Sticky placement |

### 2.5 Roles

| Term | Meaning |
|---|---|
| Person | The human who uses Shepherdr. |
| Assistant | The agent that runs tasks for the person in one space. |
| Worker | The agent that does one Task. |
| Reviewer | The read-only agent that checks a worker's changes. |
| Space | A Herdr space. It has its own settings and its own Assistant. |

## 3. Decisions made

Each decision came from a question put to the person.

| Area | Decision |
|---|---|
| Model | A Feature is a separate record (ADR-0001). Tasks keep integer ids and have a `feature` link (ADR-0002). |
| Approval | The Accept of the spec Task is Plan approval. A plan change goes through an Amendment. |
| Merging | Child Tasks merge by themselves, after checks. A Risky Task needs the person's Accept. |
| Risk | A row is risky when the `risk` column says `high` or when `owns` matches a risky path. |
| Parallel safety | `ready` refuses a child Task whose diff goes outside its owned paths. |
| Review | A clean rebase keeps a recorded review. The patch-id decides. |
| Building | Fan-out runs and starts the children by itself after Plan approval. The queue does not change. |
| Failure | A failed or stalled child gets one retry. Then it blocks only its own dependents. |
| Verify | The Feature allows two fix rounds. Then it asks the person. |
| Closing | `herdr-task` ticks the rows, archives on the feature branch, merges the base in, and opens the pull request. GitHub squash-merges it. |
| Base conflict | A conflict with the base creates a risky Integration Task. |
| Remote | The flow works with and without a GitHub remote. Local mode warns and is limited to one machine. |
| Machines | The Feature lives on the home machine. Children run on any machine, with chain affinity. |
| Assistant news | The Assistant gets summaries of routine events. It gets exceptions one by one. |
| Retro | One retro for each Feature. It reads digests. It saves outside the repo by default. |
| Skills | `flow init` runs `openspec init`. It asks before it adds a missing Matt Pocock skill. |
| UI | One grouped Feature row, with two actions. No Feature page in the first version. |
| Process | This file comes first. No code before the person approves it. No README change. No release. |

## 4. Scope

In scope:
- The `flow` setting and the Feature record.
- Instruction files for the spec Task, the child Task, the Verify Task and the retro.
- The commands `feature`, `fanout`, `sync-branch`, `feature-status`, `retro-data`, `retro` and `flow init`.
- The new `add` flags `--feature`, `--owns` and `--risk`.
- Fan-out, closing, failure handling and digests.
- Settings rows in the Herdr window and in the web app.
- A grouped Feature row in both user interfaces.
- Tests and docs.

Not in scope:
- Changes to the classic flow. It stays the default and works as before.
- Changes to herdr-projects code.
- A new merge queue service.
- A Feature page with a wave graph.
- Support for remotes other than GitHub. Such a repo uses local mode.
- A fairness cap between two Features in one folder.

## 5. How Shepherdr works today

These facts come from the code. They limit the design.

- `herdr-task add` accepts `--branch`, `--base`, `--after` and `--set`.
- A Task in a worktree gets its own branch. Its pull request goes to its base branch.
- A worker says `ready`. The Task then waits in Ready for the person to accept it.
- `finish` removes the worktree and deletes the local branch of a merged Task.
- A worktree branch with no commits of its own is done at once, with nothing to merge.
- Merge has three values: `ask`, `checks` (merge when checks pass) and `auto` (merge at once).
- A worker records a review round with `herdr-task reviewed`. The record holds the commit hash. `ready` is refused if no round is recorded on the final commit.
- The Assistant merges a pull request by squash.
- Each folder has one store file with one counter (`next`). The store is locked while it changes.
- The queue starts waiting Tasks in the order of `queued_at`. `max_running` limits running Tasks for each folder. A busy machine can also hold new Tasks back.
- A Task can wait for a Task on another machine (`--after vm-b:HAT-12`). The queue asks the other machine at most every 60 seconds, and at most 3 machine calls in one tick.
- The worker template says: do not rebase shared branches.
- Settings flow through `settings.py`, `core/settings.js`, `service/settings.ts`, `web/src/Settings.tsx` and `mods/shepherdr-settings`.
- `MAX_RUNNING` is 10 by default.
- Role templates live in `templates/`. A user can edit a copy in `~/.config/herdr-core/templates`. `templates.py` keeps a chain of old built-in texts, so that an upgrade can tell an unchanged copy from an edited one.
- This repo has no `openspec/` folder. Shepherdr needs `flow init` before it can use the flow on itself.
- OpenSpec CLI 1.3.1 is installed here. It has `openspec validate [item] --json` and `openspec archive [change] -y`.
- The skill `/opsx:apply` changes `- [ ]` to `- [x]` in `tasks.md` as it finishes a row.

## 6. Design

### 6.1 The flow switch and `flow init`

- The setting `flow` has the values `classic` and `openspec`. The default is `classic`.
- It works for a space and for one Task (`--set flow=openspec`). It shows in the Settings block at the end of each brief.
- The setting changes what a Feature does. It does not turn every Task into a Feature (section 6.2).
- When `flow=openspec`, `brief()` adds one flow file after the normal worker words. When `flow=classic`, the brief does not change.
- The built-in `worker.md`, `reviewer.md` and `assistant.md` get one short pointer each. A pointer is active only when the Settings block says `Flow: openspec`.
- `herdr-task flow init` prepares a repo:
  1. It runs `openspec init` without asking.
  2. It looks for each Matt Pocock skill at repo level (`.claude/skills`) and at user level (`~/.claude/skills`), on the machine where the worker runs. The skills are `grill-with-docs`, `grilling`, `domain-modeling`, `to-spec`, `retro` and `writing-for-agents`.
  3. It asks the person only for a skill that is missing at both levels. After a yes, it copies the skill into the repo, pinned to a source commit. It keeps the MIT notice.
  4. It commits the result as an ordinary pull request for the person to review.
- The Matt Pocock repo uses the MIT license. `writing-for-agents` is in `skills/productivity/`. The `retro` skill is in `skills/engineering/retro/`.
- `flow init --update` moves the pinned copies to a newer commit.

### 6.2 Lifecycle

```
Start     The person asks for a Feature, or the Assistant proposes one and the person says yes.
          herdr-task feature add "<title>" creates the Feature, the feature branch and the spec Task.

Stage 1   PLAN      The spec worker asks: path A or path B?
                    A: /opsx:explore, then /opsx:propose
                    B: /grill-with-docs, then /to-spec (into the change, not the issue tracker), then /opsx:propose
                    The change has a work table in tasks.md.
                    ready is refused until `openspec validate` and `fanout --dry-run` pass.
                    The Accept card shows the dry-run summary.
                    The person accepts: Plan approval.

Stage 2   FAN-OUT   herdr-task runs fanout and starts the children. The Assistant gets one digest.

Stage 3   BUILD     Children work in parallel, each on its own branch.
                    Each child is reviewed by its worker's reviewer.
                    Each child merges into the feature branch after checks.
                    A Risky Task waits for the person's Accept.

Stage 4   VERIFY    A Verify Task runs /opsx:verify on the feature branch.
                    Findings become Fix Tasks. The Feature allows two fix rounds.
                    After the second round with open findings, the Feature shows Needs you.

Stage 5   CLOSING   herdr-task ticks the rows, archives the change on the feature branch,
                    merges the base branch in, and opens the pull request.
                    The person presses Merge feature. GitHub squash-merges the pull request.

Stage 6   RETRO     One retro for the Feature, if the setting asks for it.
```

The person gives input in two places only: at the start (stage 1) and at the end (stage 5).
The person also accepts each Risky Task, and each Amendment.
A small request stays an ordinary Task, also in a space with `flow=openspec`.

### 6.3 The Feature record

A Feature is a record in the store of its folder. It is not a Task.

| Field | Meaning |
|---|---|
| `id`, `ref` | The number from the folder counter, and its ref (`HAT-12`). |
| `title`, `change` | The title, and the name of the OpenSpec change. |
| `branch`, `base` | The feature branch, and the base branch. |
| `mode` | `remote` or `local`. It is fixed when the Feature is created. |
| `phase` | One of the phases in section 2.3. |
| `attention` | The reason for Needs you, or empty. |
| `spec_task` | The ref of the first spec Task. |
| `children` | The refs of the child Tasks, with the machine of each. |
| `fix_rounds` | The number of fix rounds used. |
| `digests` | The digests of ended children (section 6.14). |

Phase rules:
- `planning`: the spec Task exists and is not accepted.
- `building`: Plan approval is done. Children run. The Feature has no agent of its own.
- `verifying`: all children are merged or skipped. A Verify Task runs.
- `closing`: verify is clean. `herdr-task` runs the closing steps (section 6.11).
- `ready`: the pull request is open. The person presses Merge feature.
- `done`: the pull request is merged. `cancelled`: the person cancelled the Feature.

The Feature has two human actions: **Merge feature** (phase `ready`) and **Add a fix round** (when the Feature shows Needs you after two fix rounds).
Accept on a spec Task, a Risky Task or an Integration Task keeps its normal meaning: merge the work.
The worktree and the branch of the Feature stay until the Feature is `done` or `cancelled`.
A spec Task and its children branch from the feature branch, so `finish` removes only their own branches.

### 6.4 Refs and branch names

- A Task keeps an integer id. A Feature takes its number from the same counter. So `HAT-12` is either a Feature or a Task, never both.
- Each Task has a `feature` field that holds the Feature id. The CLI and both user interfaces use it to group and show Tasks, for example `HAT-12 › HAT-14`.
- Feature branch: `feature/<SLUG>-<NUM>-<short-title>`. Example: `feature/HAT-12-login-flow`.
- Task branch: `task/<FEATURE-REF>-<CHILD-REF>-<short-title>`. Example: `task/HAT-12-HAT-14-parser`.
- Names go in through `--branch`. The existing check for a used branch name stays active.
- A child uses `--base feature/<...>`.

### 6.5 The work table

The spec worker adds this table to `tasks.md`. `fanout` reads it. The format is strict.

```
| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 1.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 2.1 | Parse headers       | src/parser/header.ts | 1.1     | low  | S    |
| 2.2 | Parse body          | src/parser/body.ts   | 1.1     | high | M    |
```

- `id` is the id of the matching checkbox line in `tasks.md` (for example `- [ ] 2.1 Parse headers`). Closing uses it to tick the box.
- `owns` lists paths or globs. A comma separates them.
- `depends` lists row ids. A comma separates them.
- `risk` is `low` or `high`. `size` is `S`, `M` or `L`. A row of size `L` must be split.
- Wave 0 holds shared contracts: types, interfaces, schemas and test seams.

### 6.6 Risk

- A row is risky when `risk` is `high`, or when one of its `owns` paths matches a risky path.
- A setting holds the risky paths for each space. The default is an empty list.
- `fanout` gives a risky child `merge=ask`. It gives all other children `merge=checks`.
- `fanout --dry-run` shows the reason for each risky child.
- The person reads the risk marks when they accept the spec Task.

### 6.7 The `fanout` command

`herdr-task fanout <change> [--dry-run] [--machines a,b,c] [--amend] [--folder <path>]`

Steps:
1. Read `openspec/changes/<change>/tasks.md`. Parse the work table.
2. Check it. Stop with a clear error for a missing id, an unknown dependency, a cycle, an empty `owns`, a size `L` row or a bad `risk` value.
3. Add a dependency between two rows whose `owns` overlap and that have none. Keep the table order.
4. Compute waves with a topological sort.
5. Group rows into work packages. A package has rows of one wave and one directory.
6. Set the order: wave first. In a wave, the longest chain of dependents first, then larger size first, then table order. The same table always gives the same order.
7. Place the packages (section 6.13).
8. Run `add` for each package, in that order, with `--feature`, `--base`, `--branch`, `--after`, `--owns`, `--set merge=<checks or ask>` and a generated brief. Then start the Tasks. The queue starts them in order as slots free up.
9. Add the first Verify Task. It waits for all children.
10. Print the plan: packages, waves, machines, risk reasons and the exact `add` flags.

Properties:
- The same input gives the same output.
- A second run skips children that exist. It adds only missing ones.
- `--dry-run` creates nothing.
- Plan approval runs `fanout` by itself. `--no-auto-fanout` on `feature add` turns that off for one Feature.

Amendment (`--amend`):
- It reads the updated table after the Amendment is accepted.
- It adds only new rows as children.
- It cancels the child of a removed row if the child has not started. A child that has started stays, and the Feature row shows a warning.
- It never changes `owns` of a running child. It prints the difference, and the Assistant decides.

### 6.8 Child worker rules (`flow-task.md`) and owned paths

- Do only your work package. Change only your owned paths.
- Do not edit `tasks.md`. Do not tick boxes. Skip the tick step of `/opsx:apply`.
- Run `/opsx:apply` for your rows only.
- Run `herdr-task sync-branch` before `ready` and when the feature branch moves.
- If you need a file that is not yours, ask the Assistant to extend your owned paths. Set the status line to "needs plan change" or "needs owns change".
- Never push to main.

Enforcement:
- `herdr-task ready` compares the diff against the base. If a changed path is outside the owned paths, `ready` is refused and lists the paths.
- A short default list is exempt: lockfiles and generated files such as `bun.lock`. A repo can edit the list.
- `sync-branch` runs the same check before it rebases.
- The Assistant can extend owned paths. It then adds `--after` links to any running child that owns the same path. The extension is recorded on the Task.
- Classic Tasks have no owned paths. The check does not apply to them.

### 6.9 Branch sync and review validity

For 40 to 50 agents, "behind the base" is the normal case. These rules make it safe.

1. A task branch belongs to one worker. The worker may rebase it and push it with `--force-with-lease`.
2. Nobody rewrites the feature branch. The base branch enters it by merge.
3. `herdr-task sync-branch` does the work. It does not depend on the judgment of the model.
4. It makes a backup ref `backup/<branch>-<time>` before the rebase. It turns on `rerere`.
5. It fetches and rebases onto the feature branch (remote mode) or onto the local feature branch (local mode).
6. On a conflict, it lists the files and marks those outside the owned paths. Then it aborts the rebase and restores the branch.
7. It never merges lockfiles or generated files by hand. It reports them so the worker can regenerate them.
8. After a clean rebase, it runs the project checks for the changed paths.

Review validity:
- `herdr-task reviewed` stores the patch-id of the whole diff against the base, next to the commit hash.
- `ready` accepts a final commit when a recorded round has the same commit hash or the same patch-id.
- A clean rebase keeps the diff the same, so the review stays valid.
- Conflict resolution, a regenerated file or a new commit changes the patch-id. The worker then needs a new review round.
- A clean rebase can still interact with newly merged work. CI, the checks that `sync-branch` runs, and the Verify Task cover that.

### 6.10 Merge modes

| | Remote mode | Local mode |
|---|---|---|
| Condition | GitHub remote and `gh` access | No GitHub remote |
| Child merge | Pull request, squash-merged into the feature branch | Fast-forward into the feature branch, under a repo lock. If it fails, the child syncs again. |
| Feature merge | Pull request, squash-merged into the base branch | Squash merge into the base branch, as the classic local merge does |
| Machines | Any number | One machine only |
| Start | Normal | `feature add` prints "No GitHub remote: local mode, one machine only" |

- Local mode refuses to place a child on another machine.
- A merge queue on `feature/*` is the best serializer in remote mode. Without one, a child syncs again after "behind" news.

### 6.11 Verify, fix rounds and closing

Verify:
- A Verify Task works on a branch of the feature branch with no commits of its own. It runs `/opsx:verify <change>`. It is read-only.
- It reports with `herdr-task verify-report --pass` or `--findings-file <file>`.
- On findings, the Assistant adds Fix Tasks with `--feature`, `--owns` and `--set merge=checks`. Then it adds a new Verify Task that waits for them. That is one fix round.
- After the second fix round with open findings, the Feature shows Needs you.
- A blocker finding that the Assistant cannot turn into a fix stops the loop at once.
- The person can add one more round, fix by hand, or merge as is.

Closing (phase `closing`), in this order:
1. Tick every row whose child merged. Make one commit on the feature branch. If a row has no merged child, the Feature shows Needs you.
2. Run `openspec archive <change> -y` and commit the result on the feature branch.
3. Merge the base branch into the feature branch. If it conflicts, abort and add an Integration Task (below). Run verify again after that Task merges.
4. Open the pull request into the base branch. The Feature moves to `ready`.

Integration Task:
- It resolves the conflicts on its own branch, with the feature branch as its base.
- It is always risky. The person must accept it.
- `herdr-task feature sync HAT-12` takes the base branch into the feature branch early. It keeps the final merge small.

The archive commit is in the squash commit that lands in the base branch. The base branch never gets a separate push.

### 6.12 Failure handling

- A failed or stalled child restarts once, from its own branch, with a note about the cause. This uses the existing resume path.
- After that, the Feature shows Needs you. The event reaches the Assistant one by one.
- The failure blocks only the children that depend on it. Other chains keep running.
- The person can retry with a new instruction, replace the row with an Amendment, or mark the row skipped. A skipped row unblocks its dependents. The Feature keeps a record of the gap, and `/opsx:verify` reports it.
- Cancelling a Feature stops running children and removes queued ones. It keeps the feature branch. It deletes nothing without the person's yes.

### 6.13 Several machines

- The Feature record lives on the home machine. That machine runs `fanout` and creates children with `herdr-task --on <machine> add`.
- `fanout --machines a,b,c` places packages with chain affinity. A chain stays on one machine, so most `--after` links are local. Independent chains spread across machines.
- Only links between waves cross machines. They use the existing `--after machine:REF`. The call budget for Feature children can be raised without a change for classic Tasks.
- `herdr-task feature-status HAT-12 --on <machine>` returns the states and digests of all children of that Feature on that machine. The home machine asks once for each machine in each tick.
- Remote mode is required for more than one machine. Each child fetches the feature branch from GitHub.

### 6.14 Assistant news and digests

- A routine child event is not sent to the Assistant. Routine events are: started, ready and merged by itself. The Feature counts them.
- The Assistant gets one digest message when a wave ends, and one for each phase change.
- It gets these events one by one: needs-you, failed, stalled, "needs plan change", a request for owned paths, a Risky Task that waits for the person, and verify findings.
- The Assistant can read the full state with `herdr-task feature show HAT-12`.
- The classic flow does not change.
- When a child ends, its machine writes a digest into the Task. The digest holds: review findings by level, needs-you and stall events, rebase conflicts, review rounds used, time against the estimate, the most expensive tool calls, and the transcript path.
- `feature-status` brings the digests to the home machine.

### 6.15 Retro

Source: https://github.com/mattpocock/skills/tree/main/skills/engineering/retro

What that skill does:
- It suggests improvements to the environment of the coding agent.
- It reads session logs.
- It checks seven categories: Navigation, Automated checks, Coding standards, Global AGENTS.md, Tool economy, No-ops and Information access.
- It shows candidates in order of severity. It changes nothing.
- It loads the skill `writing-for-agents` first.

How Shepherdr uses it:
- The retro is a Task with its own role template, `templates/retro.md`. The retro agent is read-only.
- It runs once for each Feature, after the Feature is `done`. It does not run for each child.
- `herdr-task retro-data <feature>` gives the agent the digests of all children as JSON. It also gives the state of the repo check script and CI.
- The agent can read one raw transcript with `hc file read --on <machine>`. It does this only for a child that the digests flag.
- The agent shows candidates as cards in the Assistant chat, in order of severity. The person accepts or declines each one.
- An accepted candidate becomes an ordinary follow-up Task.
- The report is saved outside the repo by default.
- The retro starts by itself when Retro is `automatic`. With `ask me`, the Assistant asks first. `herdr-task retro HAT-12` starts it at any time.
- The reviewer template also reads `CODING_STANDARDS.md` if the repo has one. A retro finding in the category Coding standards feeds that file.

### 6.16 Settings

All new settings work for a space and for everywhere. They default to "off" or "ask".

| Setting | Values | Default |
|---|---|---|
| Flow | classic, openspec | classic |
| Verify rounds | 1 to 5 | 2 |
| Risky paths | a list of globs | empty |
| Retro | off, ask me, automatic | off |
| Retro prompt | built-in, or a skill name or file path | built-in |
| Retro agent | CLI, model, effort | the Reviewer's |
| Retro saves to | outside the repo, repo (`docs/retros/`), pull request comment | outside the repo |
| May change without asking | nothing, `AGENTS.md`, specs and ADRs | nothing |

Both user interfaces read the same `shepherdr.json`.

### 6.17 User interface

- In the Tasks list of the window and of the web app, a Feature is one group row. Its Tasks are nested under it.
- The row shows the phase, the number of merged children out of the total, and the open verify findings.
- The row has the two actions: Merge feature and Add a fix round.
- Risky Tasks show their normal Accept card. Other children make no cards.
- The settings rows are in the Tasks page.
- The CLI shows the full detail: `herdr-task feature show HAT-12` and `herdr-task list --feature HAT-12`.

### 6.18 Scale to 40 to 50 agents

- Raise `max_running` for the space. Do not change the global default in this work.
- The queue stays first in, first out. `fanout` sets the creation order.
- Measure the store lock. 50 workers write status to one file. A wait of more than 200 ms is a problem.
- Check machine limits (memory, CPU, disk) in Settings > Machines.
- Check the rate limits of the model. Ramp up in steps: 10, 25, then 50. Measure the rebase conflict rate at each step.

## 7. Rules that must not break

- A space with `flow=classic` behaves as before. A test proves it.
- herdr-projects commands and contracts do not change.
- Task ids, mod names, layout node ids and `keys.json` contexts do not change.
- The format of the store file stays readable by the old code. A Feature is a new list. Old code ignores it.
- `bin/sync-shared --check` passes.
- Tests use throwaway roots only. They never use the real `~/.config/herdr-app`.
- Never run `herdr-app check` on `shepherdr-settings`. Its init pushes settings to other machines.
- A test never runs a changed herdr-task on other machines.
- The window UI cannot be shown on the dev VM. A person checks it. The web UI is checked with the Bun stub.

## 8. Risks

| Id | Risk | Reduction |
|---|---|---|
| R1 | The Feature record is a new part of the store and of both user interfaces. | One new list in the store. Old code ignores it. Small first UI. ADR-0001. |
| R2 | The spec worker writes a bad work table. | `ready` refuses a plan that fails `openspec validate` or `fanout --dry-run`. The spec template has an example. |
| R3 | Many merges into one feature branch cause "behind" loops. | `sync-branch`. A merge queue where possible. Small packages. Owned paths. |
| R4 | The store lock is slow with 50 agents. | Measure first (T09). Fix in a follow-up task if the wait is over 200 ms. |
| R5 | A skill is missing on a machine. | `flow init` checks at repo and user level and asks before it adds a copy. |
| R6 | The built-in template upgrade chain does not match. | T14 adds the chain entry and a test in `test_repo.py`. |
| R7 | The retro list is long and noisy. | Order by severity. Cap the list. The person approves each item. |
| R8 | Cross-machine polling is slow. | Chain affinity and one batched `feature-status` for each machine in each tick. |
| R9 | A wrong `owns` list blocks a child. | The refusal is clear. The Assistant can extend the paths. The block shows early, not as a merge conflict. |
| R10 | A clean rebase keeps a review that no longer fits the new base. | CI, post-rebase checks and the Verify Task. This is a chosen trade. |

## 9. Work breakdown

Each task owns a set of files.
Tasks in one wave do not own the same file.
New tests go in new test files, so tasks do not conflict in `test_herdr_task.py`.
Sizes: S is under 1 hour, M is 1 to 3 hours, L is more than 3 hours.
The task numbers T00 to T25 belong to this proposal. They are not Shepherdr refs.

### 9.1 Dependency map

```
Wave 0:  T00
Wave 1:  T01  T02  T03  T04  T05  T06  T07  T08  T09  T10        (10 in parallel)
Wave 2:  T11  T12  T13  T14  T15  T16  T17  T18                  (8 in parallel)
Wave 3:  T19  T20  T21  T22                                      (4 in parallel)
Wave 4:  T23  T24  T25                                           (3 in parallel)
```

### 9.2 Table

| Id | Title | Depends | Size | Wave |
|---|---|---|---|---|
| T00 | Contract document | none | S | 0 |
| T01 | Python settings: flow, verify rounds, risky paths, retro | T00 | M | 1 |
| T02 | Shared JS settings: rows and text | T00 | M | 1 |
| T03 | Flow and retro instruction files | T00 | M | 1 |
| T04 | Store: shared counter and Feature records | T00 | M | 1 |
| T05 | `fanout` module and plan check | T00 | L | 1 |
| T06 | Ready guards: owned paths and patch-id | T00 | M | 1 |
| T07 | `sync-branch` module | T00 | M | 1 |
| T08 | Digest and `retro-data` modules | T00 | M | 1 |
| T09 | Scale measurement | T00 | M | 1 |
| T10 | `flow init` module | T00 | M | 1 |
| T11 | Feature lifecycle and `feature` commands | T01, T04 | L | 2 |
| T12 | Herdr window settings rows | T02 | M | 2 |
| T13 | Web settings rows | T02 | M | 2 |
| T14 | Role template pointers and upgrade chain | T03 | S | 2 |
| T15 | Flow injection in `brief()` | T01, T03 | M | 2 |
| T16 | Closing steps module | T04, T05 | L | 2 |
| T17 | `feature-status` module | T04, T08 | M | 2 |
| T18 | `retro` command | T01, T03, T08 | M | 2 |
| T19 | Feature tick: phases, fan-out trigger, retries, news | T05, T08, T11, T16, T17 | L | 3 |
| T20 | Command and CLI wiring | T05, T06, T07, T10, T11, T16, T17, T18 | L | 3 |
| T21 | Feature row in the web app and service | T04, T11 | M | 3 |
| T22 | Feature row in the Herdr window | T04, T11 | M | 3 |
| T23 | End-to-end test with fake agents | T19, T20 | L | 4 |
| T24 | Final docs | T19, T20 | M | 4 |
| T25 | Scale ramp and report | T19, T20 | M | 4 |

### 9.3 Task details

#### T00 Contract document
- Goal: fix every shared name and format before parallel work starts.
- Owns: `docs/openspec-flow.md` (contract section only).
- Content: setting keys and values; the Feature record fields and phases; the Task fields `feature`, `owns`, `risk`, `retries`, `skipped`, `digest` and `patch_id`; the work table grammar; the JSON of `fanout --dry-run`, `feature-status`, `retro-data` and the digest; the exit codes and messages of `sync-branch`, `verify-report` and the `ready` guards.
- Done when: the person approves the document. Every wave 1 task cites it.

#### T01 Python settings
- Owns: `mods/tasks/bin/herdr_task/settings.py`, `mods/tasks/bin/herdr_task/assistant_settings.py`, `test/test_flow_settings.py`.
- Add the keys to `DEFAULTS`. Add Settings block lines for Flow and Retro. Add the same facts to the "Your settings" block of the Assistant.
- Done when: `settings_for` returns the new values for each space. A Task with `--set flow=openspec` shows the line. Existing output does not change.

#### T02 Shared JS settings
- Owns: `core/settings.js` (or its `lib/` source) and the copies that `bin/sync-shared` makes.
- Add `DEFAULTS`, `ABOUT` text, row labels and `TASK_ROWS` entries.
- Done when: `bin/sync-shared --check` and `node --check` pass.

#### T03 Flow and retro instruction files
- Owns: new files `templates/flow-spec.md`, `templates/flow-task.md`, `templates/flow-verify.md`, `templates/retro.md`.
- Include the work table example, the rules of section 6.8, the sync rules and the "present, do not apply" rule of the retro.
- Done when: each file has a header like the existing templates. `test_repo.py` passes.

#### T04 Store: shared counter and Feature records
- Owns: `mods/tasks/bin/herdr_task/store.py`, `test/test_feature_store.py`.
- Add a `features` list to the store file. Draw a Feature number from the same counter. Resolve a ref to a Task or a Feature. Add the `feature` field to a Task.
- Done when: old code still reads a file that has a `features` list. A ref never matches both a Feature and a Task.

#### T05 `fanout` module and plan check
- Owns: new `mods/tasks/bin/herdr_task/fanout.py`, `test/test_fanout.py`.
- Implement section 6.7: parse, check, waves, order, placement, `--amend` and the plan check (`openspec validate` and the dry run).
- Done when: tests cover valid tables, cycles, overlaps, repeat runs, `--dry-run`, ordering, placement and amendments. A real throwaway git repo is used. Only `hc` and `gh` are mocked.

#### T06 Ready guards
- Owns: new `mods/tasks/bin/herdr_task/ready_guards.py`, `test/test_ready_guards.py`.
- Implement the owned-paths check with the exempt list, and the patch-id calculation and match.
- Done when: tests cover a diff inside and outside the owned paths, exempt files, a clean rebase with the same patch-id and a changed patch-id.

#### T07 `sync-branch` module
- Owns: new `mods/tasks/bin/herdr_task/branchsync.py`, `test/test_branchsync.py`.
- Implement section 6.9, rules 4 to 8, for remote and local mode.
- Done when: tests in a throwaway repo cover a clean rebase, a conflict inside and outside the owned paths, a lockfile conflict and an abort that restores the branch.

#### T08 Digest and `retro-data` modules
- Owns: new `mods/tasks/bin/herdr_task/digest.py`, `mods/tasks/bin/herdr_task/retro_data.py`, `test/test_digest.py`, `test/test_retro_data.py`.
- Build the digest of section 6.14 from a Task. Build the retro JSON from the digests of a Feature.
- Done when: a real log replayed through `core/chat-log.js` gives the events and the transcript path. A Feature with no children gives a valid result.

#### T09 Scale measurement
- Owns: `test/test_scale.py`.
- Start 50 fake workers that write status at once on the current store. Measure the lock wait and the queue behavior.
- Done when: a written result lists the numbers. If the wait is over 200 ms, a follow-up task for `store.py` is added to this list.

#### T10 `flow init` module
- Owns: new `mods/tasks/bin/herdr_task/flow_init.py`, `test/test_flow_init.py`.
- Implement section 6.1: `openspec init`, the skill check at repo and user level, the ask, the pinned copy with the MIT notice, and `--update`.
- Done when: tests cover a skill at repo level, at user level and at neither level. The ask happens only in the last case.

#### T11 Feature lifecycle and `feature` commands
- Owns: new `mods/tasks/bin/herdr_task/feature.py`, `test/test_feature.py`.
- Implement `feature add`, `show`, `list`, `sync` and `cancel`; the phase rules of section 6.3; the mode check (remote or local) with its warning; and the creation of the feature branch and the spec Task.
- Done when: tests show each phase change, the local-mode guard for other machines, and that cancel deletes nothing.

#### T12 Herdr window settings rows
- Owns: `mods/shepherdr-settings/main.js`, `mods/shepherdr-settings/coordinator.js`.
- Add the rows of section 6.16 to the Tasks page. Keep the "Inherited" and "Reset" behavior for each space.
- Done when: `node --check` passes. A person checks the page in the real window. Do not run `herdr-app check` on this mod.

#### T13 Web settings rows
- Owns: `service/settings.ts`, `web/src/Settings.tsx`.
- Add the same rows. Show "May change without asking" only when Retro is not off.
- Done when: `bunx tsc` passes. Bun stub screenshots show the rows in light and dark mode.

#### T14 Role template pointers and upgrade chain
- Owns: `templates/worker.md`, `templates/reviewer.md`, `templates/assistant.md`, `mods/tasks/bin/herdr_task/templates.py`.
- Add one pointer to the worker and the reviewer. Add an "OpenSpec flow" section to the Assistant. Add the old texts to the upgrade chain.
- Done when: a test shows that an unchanged old copy upgrades and an edited copy stays.

#### T15 Flow injection in `brief()`
- Owns: `mods/tasks/bin/herdr_task/launch.py`.
- When `flow=openspec`, add the right flow file to the brief of a spec, child or Verify Task. Stop with a clear message when the repo has no `openspec/` folder.
- Done when: tests show that a classic brief stays the same, byte for byte.

#### T16 Closing steps module
- Owns: new `mods/tasks/bin/herdr_task/closing.py`, `test/test_closing.py`.
- Implement section 6.11: tick rows by row id, `openspec archive -y`, merge the base in, open the pull request, and create the Integration Task on a conflict.
- Done when: tests in a throwaway repo cover a clean close, a base conflict and a row with no merged child.

#### T17 `feature-status` module
- Owns: new `mods/tasks/bin/herdr_task/feature_status.py`, `test/test_feature_status.py`.
- Return the states and digests of the children on one machine in one call. Read it from the home machine, one call for each machine.
- Done when: tests show one call for each machine, and that a machine that is offline does not stop the others.

#### T18 `retro` command
- Owns: new `mods/tasks/bin/herdr_task/retro.py`, `test/test_retro.py`.
- Create the retro Task with the retro agent settings. Save the report where the setting says. Handle `off`, `ask me` and `automatic`.
- Done when: tests show each setting value. Nothing happens when Retro is `off`.

#### T19 Feature tick
- Owns: `mods/tasks/bin/herdr_task/tick.py`, `test/test_feature_tick.py`.
- Run Plan approval and fan-out. Move phases. Count fix rounds. Restart a failed child once. Write digests. Send the summaries and the one-by-one events to the Assistant.
- Done when: tests show each phase change, the retry, the Needs you after two fix rounds, and that classic Tasks do not change.

#### T20 Command and CLI wiring
- Owns: `mods/tasks/bin/herdr_task/commands.py`, `mods/tasks/bin/herdr_task/cli.py`.
- Add `--feature`, `--owns` and `--risk` to `add`. Add the commands of section 4. Call the ready guards and the spec-Task plan check from `ready`. Store the patch-id in `reviewed` and use it in the review check. Run the Plan approval hook on the Accept of a spec Task.
- Done when: `herdr-task --help` lists the commands. Each command runs from the CLI in a throwaway root. Existing tests pass.

#### T21 Feature row in the web app and service
- Owns: the service files that build the Tasks rows (`service/store.ts` and `core/tasks.js` are the likely ones), and the web files that group and show them (`web/src/groups.ts` and `web/src/Panel.tsx` are the likely ones).
- The first step is to confirm the exact file list in the PR description.
- Show the grouped Feature row of section 6.17, with the two actions.
- Done when: `bunx tsc` passes. Bun stub screenshots show the row in each phase, in light and dark mode.

#### T22 Feature row in the Herdr window
- Owns: the window files of `mods/tasks` that draw the Tasks list (`mods/tasks/ui.js` is the likely one).
- The first step is to confirm the exact file list in the PR description.
- Show the same row and actions.
- Done when: `node --check` passes. A person checks the row in the real window.

#### T23 End-to-end test with fake agents
- Owns: new `test/test_flow_e2e.py`.
- Run stages 1 to 6 in a throwaway repo with a fake `hc` and a fake `gh`. Include 20 children, one forced conflict, one failed child and one risky child. Include a local-mode run.
- Done when: the test passes. A classic run in the same test does not change.

#### T24 Final docs
- Owns: `docs/openspec-flow.md` (all except the contract section), `docs/settings.md`, `docs/develop.md`.
- Add the new settings to the audit table. Describe the commands, the phases and the two modes.
- Done when: `python3 -m unittest discover -s test` passes, including the repo rules.

#### T25 Scale ramp and report
- Owns: a written report in `docs/openspec-flow.md` (the scale section).
- Run the flow with 10, 25 and then 50 agents. Measure the lock wait, the rebase conflict rate and the time between waves.
- Done when: the report has the numbers for each step and says whether to go on.

## 10. Test plan

- Quick checks before every change: `bin/sync-shared --check`, `python3 -m unittest discover -s test`, and the `node --check` loop in `docs/develop.md`.
- Unit tests in new files for each task.
- A regression test for the classic flow (T15, T19, T20, T23).
- A load test with 50 fake workers (T09).
- A visual check of the web rows with the Bun stub (T13, T21).
- A person checks the Herdr window rows (T12, T22).
- GitHub runs the Check workflow on each pull request.

## 11. Rollout

1. The person approves this proposal.
2. Run wave 0, then waves 1 to 4. Each task has its own branch and pull request.
3. Run `flow init` on a small test repo. Try the flow on one small Feature with 3 to 5 children.
4. Raise the number of parallel agents to 10, 25, then 50. Measure at each step (T25).
5. Ask the person before any release.

## 12. ADR-0001: A Feature is a separate record, not a Task with phases

Status: accepted.

Context:
A Task means one worker, one branch and one pull request. Accept on a Task means "merge its work".
A Feature groups many Tasks and has its own lifecycle.

Decision:
A Feature is a record of its own in the store. The spec Task, the child Tasks and the Verify Tasks are ordinary Tasks.
The Feature has its own phase and its own two human actions: Merge feature and Add a fix round.

Considered options:
- A Feature as a Task with phases. Accept would mean "plan approved" in one phase and "merge into main" in another. The Task would have no agent while it builds. The word "Task" would then have two meanings that depend on a hidden field.

Consequences:
- Accept keeps one meaning for every Task.
- The store, the CLI and both user interfaces need a new concept. The first UI is one grouped row.
- This is hard to reverse, because later code and saved data rely on it.

## 13. ADR-0002: Integer task ids with a `feature` link, not dotted ids

Status: accepted.

Context:
The person wanted to see the lineage in the ref, for example `HAT-12.1.3` for a child of Feature `HAT-12`.
Task ids are integers in the store, in `--after` lists, in the labels `herdr.task`, in chat links, in tab titles and in both user interfaces.
Saved state depends on them.

Decision:
Task ids stay integers from the shared counter of the folder. A Feature takes its number from the same counter.
Each Task has a `feature` field. The CLI and both user interfaces show the lineage from that field, for example `HAT-12 › HAT-14`.
Branch names carry the lineage: `task/HAT-12-HAT-14-parser`.

Considered options:
- Dotted ids. They show the lineage in the ref. But they change how ids are stored, parsed and linked, in Python, JS and the web app. The place of a Verify Task or a Fix Task in the numbering is also not clear.
- A separate prefix for Features (`HAT-F3`). It needs a second counter. It does not fit the branch pattern `feature/<SLUG>-<NUM>-<short-title>`.

Consequences:
- Existing refs, `--after` lists and saved state do not change.
- A Feature uses up a number that no Task has.
- `list --feature HAT-12` shows the tree.
- A later change to dotted ids is hard. This is why the decision is recorded.

## 14. Open points

- T21 and T22 confirm the exact file list of the Tasks row code before they start.
- The merge-queue settings of GitHub differ for each repo. Remote mode works without a merge queue, but with more "behind" retries.
- `openspec validate --json` output is used as it is in OpenSpec 1.3.1. T05 pins the checked behavior in a test.
- T09 can show that the store lock is too slow. Then a follow-up task for `store.py` goes into section 9.
