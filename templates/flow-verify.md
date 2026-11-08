---
name: flow-verify
description: Added to a worker's brief when flow=openspec and the Task is the Verify Task of a Feature. It tells the worker to run /opsx:verify read-only on the feature branch and report the result with herdr-task verify-report.
command: claude
---
OpenSpec flow: your Task is the Verify Task of a Feature.

You check the whole feature branch against its Change, read-only.
Your branch has no commits of its own.
You write no code, you make no commits, and you fix nothing.

## Run verify

- Run `/opsx:verify <change>` on the feature branch (the change name is in your brief).
- Stay read-only: do not edit files, do not commit, do not push, do not fix a finding. Findings become Fix Tasks that other workers do.

## Report the result once

Report exactly once, so the Feature can move on:

- All clean: `herdr-task verify-report --pass`.
- Findings: write them to a JSON file, then run `herdr-task verify-report --findings-file <file>`.

The findings file is a JSON list, one object per finding:

```json
[
  { "level": "blocker", "title": "Auth bypass", "detail": "why and where", "paths": ["src/auth.ts"], "row": "2.2" },
  { "level": "major",   "title": "Missing check", "detail": "...",          "paths": [],              "row": null }
]
```

- `level` is `blocker`, `major` or `minor`.
- `paths` and `row` may be empty or `null` when a finding has no single place.
- A skipped row is a gap: `/opsx:verify` flags it, so include it as a finding.

The Assistant turns findings into Fix Tasks and starts a new Verify Task: that is one fix round.
A Feature allows a set number of fix rounds (the Settings block says how many); after that it asks the person.
A blocker you report that cannot become a fix stops the loop at once.
