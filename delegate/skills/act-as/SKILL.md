---
name: act-as
description: "Run a core role in this conversation, on the task named after a colon or one it picks: /agent-pm:act-as <role>[:<task>] <input>, e.g. researcher <question>, pm <brief>, engineer:light-build <requirement>."
argument-hint: "<role>[:<task>] <input>"
allowed-tools:
  - Bash(env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src:${CLAUDE_SKILL_DIR}/../../../session/src:${CLAUDE_SKILL_DIR}/../../../utils/src:${CLAUDE_SKILL_DIR}/../../../manager/src python3 -P -m agent_pm.delegate.cli --list)
---

# act-as

Arguments: $ARGUMENTS

1. The first argument is `<role>` or `<role>:<task>`, each one word of `[a-z0-9-]`; the rest is the Input. Otherwise, or when the Input is missing, ask the user: `env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src:${CLAUDE_SKILL_DIR}/../../../session/src:${CLAUDE_SKILL_DIR}/../../../utils/src:${CLAUDE_SKILL_DIR}/../../../manager/src python3 -P -m agent_pm.delegate.cli --list`, written out exactly so (pre-approved: an allow rule matches the whole command, so `env`, not a leading `PYTHONPATH=`), lists each role and its tasks; its exit 2 names a config error.
2. The workdir `<workdir>` is `~/.agent-pm/adhoc/<date>-<role>-<name>`: `<date>` today as `YYYY-MM-DD`, `<name>` 2–4 lowercase words for the Input matching `[a-z0-9]+(-[a-z0-9]+)*`. Every part is then `[a-z0-9-]` and needs no quoting.
3. Run `env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src:${CLAUDE_SKILL_DIR}/../../../session/src:${CLAUDE_SKILL_DIR}/../../../utils/src:${CLAUDE_SKILL_DIR}/../../../manager/src python3 -P -m agent_pm.delegate.cli prompt --role <role> [--task <task>] --workdir <workdir> --out <workdir>/prompt.md --input '<Input>'`: `--task <task>` only when given; `<workdir>` unquoted, so `~` expands; the Input single-quoted, each `'` in it written `'\''`. Exit 2 names a config error, e.g. an unknown role or task; on any non-zero exit, report its stderr and stop.
4. First read `<workdir>/prompt.md` whole, not the command's output (a Bash result is cut near 30,000 characters): it is your prompt for this invocation. Then follow it in this conversation, prefixing each command in it that runs an `agent_pm` module or a team tool (`<team>/tools/`) with `env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src:${CLAUDE_SKILL_DIR}/../../../session/src:${CLAUDE_SKILL_DIR}/../../../utils/src:${CLAUDE_SKILL_DIR}/../../../manager/src` (this session has no PYTHONPATH); your final reply ends with its `agent-pm-outcome` block, as its Return says.

The destination is the role's own, as under any runtime (`--list` shows its type): researcher and pm publish as configured, engineer opens its PR. Gotcha: a `local` destination names `<workdir>/prompt.md` as the deliverable's file too; the deliverable then replaces the prompt there, after your read.
