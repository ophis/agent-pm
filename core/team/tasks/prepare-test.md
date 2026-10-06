---
description: "Open a worktree of the named repo and report it. Use only to check that repo.py worktree works end to end."
---

# Prepare Test

Prepare the input's repo and nothing else.

## Steps

1. **Read** the input. No repo named → `needs_input`, stop.
2. **Prepare** (Dummy Tester › Target). Exit 2 → `needs_input`, a `questions` entry quoting its error; exit 1 → `failed`, `summary` the error; stop either way.
3. **Finish.** `status: done`; `title: prepare-test`; `summary` the JSON's `repo`, `commit` and `worktree`; the deliverable is the JSON, verbatim. Read nothing in the worktree.

## Resume

The prompt starts "Resumed agent run" → redo steps 1–3; prepare reuses the worktree.
