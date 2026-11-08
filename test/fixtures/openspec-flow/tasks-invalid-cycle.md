# Cycle

A cycle: 1.1 depends on 1.2 and 1.2 depends on 1.1. fanout stops with code `cycle`.

| id  | title   | owns        | depends | risk | size |
|-----|---------|-------------|---------|------|------|
| 1.1 | First   | src/a.ts    | 1.2     | low  | S    |
| 1.2 | Second  | src/b.ts    | 1.1     | low  | S    |

- [ ] 1.1 First
- [ ] 1.2 Second
