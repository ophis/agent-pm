# Promote: Handoff to the next stage

Status: implemented (agent-pm main 92fefeb; Product Design → Engineering added on branch promote-tdd).

## Problem

After reviewing a finished stage, the user wants the next stage to start without writing its issue by hand. The user moves the issue to the team status `Handoff` (Started category, after In Review) and writes a comment saying what to build. Nothing processes `Handoff` today.

## Scope

Enabled: Deep Research → Product Design (PRD, instructions required) and Product Design → Engineering (TDD, technical design doc; `require_instructions = false`, since a PRD carries enough context). Issues in `Handoff` in a project without `next` are left alone. Out of scope: label routing and fan-out, running on GitHub Actions, webhooks.

Must extend to Product Design → Engineering (and later pairs) by config alone: nothing in the code names a project; adding `next = "Engineering"` to Product Design and a `prefix` to Engineering enables it. All stages are projects of one team.

## Rule

`scripts/promote.py`, run by its own LaunchAgent every 15 minutes, all day. No LLM, no usage gate, no tmux lock; it only touches issues in `Handoff` and the issues it creates, so it can run beside the research tick. launchd never starts a second instance of a job that is still running.

For each issue in `Handoff` whose project has a `next` in `pipeline.toml`, oldest Handoff move first:

1. **Cutoff.** The latest move to In Review whose previous state was not `Handoff` (so promote's own bounces don't count); no such move → no cutoff.
2. **Instructions.** Comments whose `user.email` is in `human_members` (comments with a null `user` are skipped), created after the cutoff, oldest first. None, and the source project does not set `require_instructions = false` → move to In Review, then comment `Handoff needs a comment saying what to build next. Moving back to In Review.` (move first, so a failed move posts nothing), next issue.
3. **Child id.** `uuid.UUID(bytes=sha256("<source uuid>/<next project id>/<first Handoff move after the cutoff, createdAt; the source's createdAt if there is none>")[:16], version=4)`: deterministic, but in the UUID v4 format `issueCreate` requires. So one review cycle has exactly one child: a re-handoff after a bounce, or of a Done issue, reuses it; a new child needs a new pass through In Review from a state other than `Handoff`.
4. **Existing child.** `issues(filter: { id: { eq: <child id> } }, includeArchived: true)` non-empty → use it; skip step 5.
5. **Create** the child with `id: <child id>` in the same team: project `next`, state Todo, no assignee, priority copied from the source, title `<prefix>: <source title>`, description:
   ```
   Handoff from <source identifier>: <source url>

   ## Source
   - <attachment title>: <attachment url>      (one line per source attachment; omitted if none)

   ## Instructions
   <user.name>, <createdAt>:
   <comment body, verbatim>                    (one block per instruction comment; section omitted if none)
   ```
   The first line is for people; nothing matches on it.
6. **Relate** the child and the source with type `related`, unless a relation between them already exists (`relations` or `inverseRelations`).
7. **Done.** Move the source to Done; comment on it `Promoted to <child identifier>.`
8. Print `<ts> promote <source> -> <child>` (or `<ts> handoff-bounce <source> no instructions`) to stdout, which launchd writes to `logs/promote.log`.

Idempotency: the child's id is fixed by the handoff, so a crash or an overlapping manual run after step 5 finds the child in step 4 (a concurrent duplicate create fails and is retried next run); steps 6 and 7 are safe to repeat. A crash between the Done move and the comment loses only the comment.

Reads: comments and history are fetched with `first: 250` and sorted by `createdAt` in code (the API returns them newest first).

Failures: anything raised while processing one issue (including `SystemExit` from `linear_gql`, network errors, and a mutation returning `success: false`) is printed and that issue is retried next run; other issues still run. If an issue's latest Handoff move is over 1 hour old and it still fails, comment `Handoff failed: <error>` (naming the child if it exists) and move it to In Review; a later re-handoff reuses that child. Failing to read `pipeline.toml` or to list `Handoff` issues, or a missing status or `next` project in Linear, exits non-zero. An issue whose own detail read fails is only logged and retried: without its history there is no Handoff time for the grace window. The detail read also re-checks the state: an issue no longer in `Handoff` (the list can lag) is skipped.

`--dry-run` prints what it would do and changes nothing.

## Config: `pipeline.toml` (repo root, tracked)

```toml
team = "Frank's Agents"
human_members = ["ophis.w@outlook.com"]   # only their comments count as instructions

[projects."Deep Research"]
next = "Product Design"

[projects."Product Design"]
prefix = "PRD"                            # title prefix for issues created in this project
next = "Engineering"
require_instructions = false              # a PRD carries enough context; comments are optional

[projects.Engineering]
prefix = "TDD"
```

Read with `tomllib` (Python 3.11+). `pick.py` keeps its own `TEAM` / `PROJECT` constants for now; moving it onto `pipeline.toml` comes with the router work.

## Logs

All logs move to `logs/` at the repo root; `.gitignore` lists `logs/` in place of `runs.log`:
- `logs/runs.log`: moved from the repo root; `RUNS_LOG` in `pick.py` and `RUNS` in `linear-research.sh` point there. It is also the runner's state (resume, attempt cap), so `logs/` must never be cleaned out.
- `logs/research.log`: the research LaunchAgent's stdout/stderr, moved from `~/Library/Logs/linear-research.log`.
- `logs/promote.log`: promote's own log; promote never writes `runs.log`.

Plists use absolute paths (`<repo root>/logs/…`). launchd does not create a missing log directory and then cannot start the job, so installing either plist starts with `mkdir -p <repo root>/logs` (README install steps). Migration, while no tick runs: `mkdir -p logs`, move `runs.log` into `logs/`, reinstall the research plist. Promote prints only when it acts, so its log grows only with handoffs; log growth is otherwise accepted.

## Code layout

- `scripts/promote.py`: `main(argv, gql=linear_gql, now=None, config=CONFIG)`, `CONFIG` = `<repo root>/pipeline.toml`. At load, every `next` must name a `projects` entry with a `prefix`, else exit non-zero; imports `linear_gql` and `parse_time` from `pick.py`; its own small state lookup and history helpers (pick.py's `Board` is bound to one project).
- `scripts/com.ophis.agent-pm.promote.plist`: `StartInterval` 900, runs `promote.py` with `/opt/homebrew/bin/python3` (macOS `/usr/bin/python3` is 3.9, without `tomllib`); stdout/stderr to `logs/promote.log`. Installed into `~/Library/LaunchAgents` like the research plist.
- README and CLAUDE.md: one entry each, and the new log paths; CLAUDE.md notes that tests need Python 3.11+.

## Tests (`scripts/tests/test_promote.py`, fake gql like `test_pick.py`)

- Happy path: DR issue in Handoff with one human comment after In Review → child created with the deterministic id in Product Design, Todo, copied priority, `PRD:` title, attachment and comment in the description; `related` relation; source Done with a comment; stdout line.
- Comment filter: agent comments, null-user comments, other users' comments, and human comments before the cutoff are excluded; several human comments are kept in order; never In Review → all human comments.
- Cutoff skips promote's bounce: bounced for no instructions, user comments and moves back to Handoff → promoted with both the old and new comments.
- No instructions → comment, In Review, no child.
- `require_instructions = false`: no comments → child without an Instructions section, source Done; comments present → still copied.
- Idempotency: child with the deterministic id exists (with and without relation) → no second child; relation created only if missing; source Done. Re-handoff after a failure bounce or from Done → same child; after a new In Review (from In Progress) → new child id.
- Child id: same inputs → same id; different Handoff move time or target project → different id.
- Config-only extension: a test config with `[projects."Product Design"] next = "Engineering"` and `[projects.Engineering] prefix = "ENG"` promotes a Product Design issue into Engineering with an `ENG:` title, no code change. A `next` without a `projects` entry or `prefix` → exit non-zero at load.
- Scope: a Handoff issue in a project without `next` is untouched.
- Failure: `SystemExit` on one issue → other issues still processed; latest Handoff move over 1h old and failing → comment + In Review; under 1h → untouched.
- `--dry-run` → no mutations.
- Dispatcher tests use `logs/runs.log`.
- Manual, once before enabling the LaunchAgent: a real handoff of a test Deep Research issue end to end (child created with the chosen id, found again on a second run), then the test issues are deleted.
