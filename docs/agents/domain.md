# Domain Docs

Single-context: one `CONTEXT.md` at the repo root, ADRs in `docs/adr/`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`**: ADRs that touch the area you're about to work in.

If either is missing, **proceed silently**: don't flag its absence or suggest creating it. `/domain-modeling` (via `/grill-with-docs` and `/improve-codebase-architecture`) creates them when terms or decisions get resolved.

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a refactor proposal, a hypothesis, a test name), use the term `CONTEXT.md` defines, never a synonym it lists under _Avoid_. A concept missing from the glossary means you're inventing language (reconsider) or found a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an ADR, say so explicitly rather than silently overriding:

> _Contradicts ADR-0003 (delegate = composer + driver), but worth reopening because…_
