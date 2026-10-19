---
name: linear
description: "Read, find, create and change Linear issues with fixed agent-pm-linear issue commands: no hand-written GraphQL."
---

# linear

Every common Linear issue operation is one `agent-pm-linear issue` command: use it, never a raw GraphQL query or a Linear MCP tool. Only an operation the table lacks (editing a description, other entities) falls back to your own Linear tool, such as a personal `/linear`.

Each command acts as the configured `harness_key` account (Keychain), needs no run on the issue, and prints one compact JSON object; diagnostics go to stderr. `<subcommand> --help` gives every option, default and output field. None is pre-approved: `read`, `list` and `lookup` write a file with `--full --output`. A client without this plugin's `bin/` on its PATH (Codex) runs `${CLAUDE_SKILL_DIR}/../../../bin/agent-pm-linear`, writing `${CLAUDE_SKILL_DIR}` as this file's directory, an absolute path.

## Commands

The prefix `agent-pm-linear issue` is left out. `ID` is an identifier (`TASK-1`) or an issue id.

| Command | Does |
|---|---|
| `read ID` | fields, newest comments and the `blocks`, `blocked_by`, `parent`, `children` columns |
| `read ID --full --output FILE` | every page and body, to FILE |
| `list [filters]` | one team's issues, newest update first; also the search |
| `lookup RESOURCE` | `teams`, `states`, `labels`, `projects`, `milestones` (`--project`), `users` with ids |
| `create --title TEXT --body-file FILE` | one issue with its fields and blocking relations |
| `move ID --state VALUE` | set its state, within its own team |
| `comment ID --body-file FILE` | a new top-level comment |
| `relation add ID --blocks OTHER` | one blocking edge; `remove` deletes it; `--blocked-by` for the other direction |
| `label swap ID --from OLD --to NEW` | replace one label, keeping the rest |
| `archive ID [--dry-run]` | archive it once it and every sub-issue are finished |

Exit 0: success, or `"result": "unchanged"` (already so; nothing written). Exit 2: usage, configuration, or a value that does not resolve; nothing was written. Exit 1: Linear failed, a refusal, a partial create, or a write whose result is unknown.

## Finding issues and values

```bash
agent-pm-linear issue list --query 'login' --state Todo --label bug
agent-pm-linear issue list --assignee me --project 'Agent PM' --include-archived
agent-pm-linear issue list --parent TASK-1 --limit 50
agent-pm-linear issue list --cursor <next_cursor>
agent-pm-linear issue lookup states --team TASK
agent-pm-linear issue lookup labels --query bug
agent-pm-linear issue lookup milestones --project 'Agent PM'
agent-pm-linear issue lookup users --query ada
agent-pm-linear issue lookup teams --full --output teams.json
```

- `list` filters all hold at once, within one team: `--team` (id, name or key), else the configured team. Archived issues only with `--include-archived`.
- A value is an id or an exact name; `--assignee` also takes an email or `me` (the `harness_key` account). No match, several, or one outside the team exits 2 naming the candidates and the `lookup` command that lists them: pick an id from it, never the first match.
- Pages: `--limit` 1–50 (default 20); `has_next_page` true → pass `next_cursor` as `--cursor` with the same options. `--full --output FILE` writes every page to FILE instead.

## Reading in full

```bash
agent-pm-linear issue read TASK-1
agent-pm-linear issue read TASK-1 --full --output TASK-1.json
```

`read` keeps 5 comments of 300 characters, 600 of the description and 10 issues per relation column. A non-empty `truncated` names what it cut; `continue` then holds the `--full --output` read that keeps everything. Read FILE for the rest.

## Writing

Bodies come from a UTF-8 Markdown file, or stdin with `-`, sent verbatim, minus a leading byte order mark: quotes, backticks, `$` and newlines need no escaping.

```bash
agent-pm-linear issue comment TASK-1 --body-file note.md
agent-pm-linear issue comment TASK-1 --body-file - < note.md
agent-pm-linear issue move TASK-1 --state 'In Progress'
agent-pm-linear issue create --title 'Fix login' --body-file note.md
agent-pm-linear issue create --title 'Fix login' --body-file - --label bug --project 'Agent PM' --milestone M1 --parent TASK-1 --assignee me --state Todo --blocks TASK-2 TASK-3 --blocked-by TASK-4 < note.md
agent-pm-linear issue label swap TASK-1 --from todo --to doing
```

- `create` resolves every value before writing; unset fields stay unset; `--state` defaults to Backlog. `--label`, `--blocks` and `--blocked-by` repeat.
- A write prints a receipt: `issue`, `operation`, `result` (`changed` or `unchanged`), `actor`, and `comment_id` for `comment`. It never echoes the body.
- `move --state` takes a state of the issue's own team, by exact name or id; a type such as `completed` is not a name.
- `label swap` refuses when the issue carries neither label, or when NEW's group already has another of its labels.

## Blocking relations

`agent-pm-linear issue relation add A --blocks B` is `agent-pm-linear issue relation add B --blocked-by A`: one edge, A blocks B. `read`'s `blocks` lists the issues it blocks, `blocked_by` the issues blocking it.

```bash
agent-pm-linear issue relation add TASK-1 --blocks TASK-2
agent-pm-linear issue relation remove TASK-2 --blocked-by TASK-1
```

Each run adds or removes that one edge in that one direction; other relations stay.

## When a write fails

- **Result unknown** (a timeout or outage mid-write, exit 1): `read` the issue before running the write again; nothing retries by itself. For `create`, first run the `list --team … --query=…` command its error prints, which searches the team it wrote to.
- **Partial create** (exit 1, `"result": "partial"`): the issue exists; `done` and `failed` list its relations, and `recover` holds the `relation add` command for each failed one. A report too long for stdout cuts `done` and `failed` before `recover`, naming each cut in `truncated`. Run only the `recover` commands; never run `create` again.

## Archiving

```bash
agent-pm-linear issue archive TASK-1 --dry-run
agent-pm-linear issue archive TASK-1
```

Archiving takes every sub-issue with it. `archive` reads all of them, at every depth, and archives only when the issue and each unarchived sub-issue are completed or canceled (state type): a Duplicate state of type `duplicate` counts as unfinished. Otherwise it exits 1 naming the unfinished ones; there is no override. `--dry-run` lists what would go, writing nothing, and exits 0 whatever it finds: read its `result` (`refused` when anything blocks) and `unfinished`. It closes no run and deletes no file.
