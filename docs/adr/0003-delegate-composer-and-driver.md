# Delegate = composer + driver over vehicle-neutral roles and tasks

Skills, agents and `claude -p` prompts are all prompts; they differ only in when text loads, which context it enters, and the runtime envelope around it. So roles and tasks stay plain sources (`.md` text + `.toml` config), not skills or agents. A composer assembles principles + role + task + input into a prompt and run config; a driver runs it. Supporting another client (Claude Code, a GPT client) means a new driver, with roles and tasks unchanged.

Hard constraints follow the driver, never the text. Config states them client-neutrally (readable dirs, writable dirs, exact allowed commands); each driver translates them into its client's enforcement. A driver that cannot enforce one refuses to run or marks it as prompt-only, never silently.

## Considered Options

- Script only, or a `/delegate` skill only: each serves one entry point and the other needs a second assembler.
- Role as a plugin agent, task as a preloaded skill: subagents cannot spawn agents or call Workflow, so deep research cannot run.
