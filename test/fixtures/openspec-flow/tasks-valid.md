# Login flow

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
