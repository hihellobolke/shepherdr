---
name: flow-spec
description: Added to a worker's brief when flow=openspec and the Task is the spec Task of a Feature. It tells the worker to write one OpenSpec change with the person, add the work table, and pass openspec validate and fanout --dry-run before ready.
command: claude
---
OpenSpec flow: your Task is the spec Task of a Feature.

You write one OpenSpec change with the person.
It becomes the plan the whole Feature builds from.
The person's Accept of your work is Plan approval: after it, Fan-out creates the Child Tasks and starts them by itself.

- Give the person input at the start only. Do not start any Child Task yourself.

## Pick path A or path B

Ask the person first: path A or path B?

- Path A, when the shape is already clear: run `/opsx:explore`, then `/opsx:propose`.
- Path B, when it needs grounding first: run `/grill-with-docs`, then `/to-spec` (write into the Change under `openspec/changes/`, never into an issue tracker), then `/opsx:propose`.

Both paths end with one Change in `openspec/changes/<change>/`: the proposal, the design, the delta specs and `tasks.md`.

## Write the work table

Add a work table to `openspec/changes/<change>/tasks.md`.
`fanout` reads it, so the format is strict.
Use exactly these six columns, in this order, with a header row and a divider row:

```
| id  | title               | owns                 | depends | risk | size |
|-----|---------------------|----------------------|---------|------|------|
| 1.1 | Shared parser types | src/parser/types.ts  |         | low  | S    |
| 2.1 | Parse headers       | src/parser/header.ts | 1.1     | low  | S    |
| 2.2 | Parse body          | src/parser/body.ts   | 1.1     | high | M    |
```

Rules:

- `id` is `<wave>.<row>`, for example `2.1`. It is unique in the table.
- `title` is free text with no `|`.
- `owns` is one or more paths or globs, comma-separated, never empty. Two rows in one wave never own the same file.
- `depends` is row ids, comma-separated, or empty. Each id is a row in the table.
- `risk` is `low` or `high`. A row that touches a risky path is high risk too, and that child merges only after the person's Accept.
- `size` is `S`, `M` or `L`. Split any `L` row into smaller rows: fan-out rejects a size `L` row.
- Wave 0 (ids `0.x`) holds the shared contracts first: types, interfaces, schemas and test seams.

Add one checkbox line per row to `tasks.md`, of the form `- [ ] 2.1 Parse headers`.
Closing ticks these boxes. Do not tick them yourself.

## Pass the plan check before ready

Both must pass, or `ready` is refused with "the plan is not valid yet":

- `openspec validate <change>`
- `herdr-task fanout <change> --dry-run`

Read the dry-run plan: the work packages, the waves, the machines and the risk reason for each risky row.
Fix the table until both pass clean.
The Accept card shows the dry-run summary, so the person reads the risk marks before they accept.

Do not run `fanout` for real and do not start any Child Task.
Plan approval runs fan-out by itself once the person accepts.
