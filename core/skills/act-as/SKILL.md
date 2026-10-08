---
name: act-as
description: "Run a core role/task in this conversation: /agent-pm:act-as <role> <task> <input>, e.g. researcher light-research <question>, pm product-design <brief>, engineer light-build <requirement>."
argument-hint: "<role> <task> <input>"
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --client skill --role *)
---

# act-as

Arguments: $ARGUMENTS

1. The first argument is the role, the second the task, the rest the Input. Role and task are each one word of `[a-z0-9-]`; otherwise, or when one is missing, ask the user (`${CLAUDE_SKILL_DIR}/../tmux/SKILL.md` › Role runs, step 1, lists the pairs).
2. Run `python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --client skill --role <role> --task <task>`, written out exactly so: that form is pre-approved. Exit 2 names a config error, e.g. an unknown role or task: report it and stop.
3. Its stdout is your prompt for this invocation: follow it.
