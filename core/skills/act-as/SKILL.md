---
name: act-as
description: "Run a core role in this conversation, on the task named after a colon or one it picks: /agent-pm:act-as <role>[:<task>] <input>, e.g. researcher <question>, pm <brief>, engineer:light-build <requirement>."
argument-hint: "<role>[:<task>] <input>"
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --client skill --role *)
---

# act-as

Arguments: $ARGUMENTS

1. The first argument is `<role>` or `<role>:<task>`, each one word of `[a-z0-9-]`; the rest is the Input. Otherwise, or when the Input is missing, ask the user (`${CLAUDE_SKILL_DIR}/../tmux/SKILL.md` › Role runs, step 1, lists each role's tasks).
2. Run `python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --client skill --role <role>`, plus ` --task <task>` only when given, written out exactly so: that form is pre-approved. Exit 2 names a config error, e.g. an unknown role or task: report it and stop.
3. Its stdout is your prompt for this invocation: follow it.
