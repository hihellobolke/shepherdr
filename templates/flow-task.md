---
name: flow-task
description: Added to a worker's brief when flow=openspec and the Task is a Child Task of a Feature. It tells the worker to stay inside its owned paths, apply only its rows, and keep its branch current with herdr-task sync-branch.
command: claude
---
OpenSpec flow: your Task is a Child Task of a Feature.

You do one work package of a Feature: the work-table rows named in your brief.
You work on your own Task branch, off the feature branch.
You merge into the feature branch, never into main.

## Stay in your work package

- Do only your work package. Change only your owned paths (the `owns` of your rows, in your brief).
- Run `/opsx:apply` for your rows only.
- Do not edit `tasks.md`. Do not tick any checkbox. Skip the tick step of `/opsx:apply`: Closing ticks the boxes, not you.
- Never push to main.
- Need a file that is not yours? Do not touch it. Set your status line to "needs owns change" (to add a path) or "needs plan change" (the plan is wrong), ask the Assistant, and wait. The Assistant extends your owned paths.

`herdr-task ready` compares your diff against the base.
A changed path outside your owned paths refuses ready and lists the paths.
A short built-in list of lockfiles and generated files is exempt, and the repo can add to it; nothing else outside your owned paths is.
The same check runs inside `sync-branch`.

## Keep your branch current

For a Feature with many agents, your branch is often behind the feature branch.
That is normal and safe when you use one command, never a hand rebase:

- Run `herdr-task sync-branch` before `ready`, and again each time the feature branch moves.

It backs your branch up, turns on `rerere`, and rebases your branch onto the feature branch.
It never touches a file outside your owned paths, and it never merges a lockfile or a generated file by hand.

Read its result and act on it (with `--json` the outcome is the `reason` field):

- `clean`: you are current. A clean rebase keeps your review valid.
- `conflict`: it aborted and restored your branch, so your working tree is clean again, with no conflict markers. The report lists the conflicted files, which of them fall outside your owned paths, and the lockfiles to regenerate. To recover: regenerate the flagged lockfiles and generated files and commit them (never merge them by hand); for a conflict in a file you own, rebase your own branch onto the feature branch yourself, resolve it only within your owned paths, and commit; then run `sync-branch` again. If any conflicted file is outside your owned paths, do not touch it: set your status line to "needs owns change" and ask the Assistant.
- `owned-paths`: your own diff already reaches outside your owned paths. Fix that first (above).
- `checks-failed`: the rebase was clean but the project checks failed on the changed paths. Fix them.

A clean rebase keeps the patch-id, so a recorded review stays valid.
Any conflict fix, regenerated file or new commit changes the patch-id.
When that happens, run your review again before `ready`, or `ready` refuses the final commit as unreviewed.

Your Merge line says how your branch merges.
A Risky Task waits for the person's Accept. Every other Child Task merges by itself after checks.
