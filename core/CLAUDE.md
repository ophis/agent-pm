# core

The portable core pack and Claude Code plugin `agent-pm` (`.claude-plugin/plugin.json`; `version` changes only when the user says): `team/` text (roles, tasks, principles, methods, templates; `guide.md` opens the prompt; what goes where: the root `CLAUDE.md` › Rules) and `output/` (how an agent run hands back), compiled by `src/compose.py` and run by `src/drive.py`. Tests: the root `CLAUDE.md` › Commands.

```bash
python3 core/src/drive.py --role R --task T --input X --out O --workdir W [--runner tui] --dry-run   # the command an agent run gets
```

## Modules

Before changing one or what it does, read its docstring (a skill: its `SKILL.md`):

- `src/compose.py`: the prompt and run config; before writing a task or charter (frontmatter, progress marks, `## Resume`).
- `src/drive.py`: an agent run's cwd and access, runners, tui give-up, `--detach`, `run.json`.
- `src/clients/`: adding a client (`__init__.py`); the `claude` command and what it needs (`claude.py`).
- `src/report.py`: the channel a run reports progress and its outcome on.
- `src/tui_claude.py`: claude in tmux: hooks, events, pane placement, live checks.
- `src/repo.py`: checkouts, worktrees, symlinks, config loading.
- `skills/`: `tmux` (workers and role runs in panes; `scripts/workers.py`), `act-as`, `manage`.

## Rules

- Core is tracker-agnostic: it knows nothing of Linear or a fixed docs repo (`docs/adr/0001`), and its prompts and `skills/` never name Linear (`compose_test.py` checks).
- Only `core/` is installed, copied per version to `~/.claude/plugins/cache/agent-pm/agent-pm/<version>/`: a skill or script references nothing outside it, a skill naming core's files by `${CLAUDE_SKILL_DIR}/../../…`, the only variable its `allowed-tools` rules get.
- `team/` and `output/` say what an agent run does and reports, client-neutrally; how a client starts a run and gets its outcome and progress back lives in that client's class.
