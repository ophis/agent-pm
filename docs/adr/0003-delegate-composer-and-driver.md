# Delegate = composer + driver over vehicle-neutral roles and tasks

Skills, agents and `claude -p` prompts are all prompts; they differ only in when text loads, which context it enters, and the runtime envelope around it. So roles and tasks stay plain sources (`.md` text + `.toml` config), not skills or agents. A composer assembles principles + role + task + input into a prompt and run config; a generic driver runs it through a client object. Supporting another client (Claude Code, a GPT client) means a new client class plus its data file, with the driver, roles and tasks unchanged.

Hard constraints follow the driver, never the text. Config states them client-neutrally (readable dirs, writable dirs, exact allowed commands); each client translates them into its own enforcement. A client that cannot enforce one refuses to run or marks it as prompt-only, never silently.

## Considered Options

- Script only, or a `/delegate` skill only: each serves one entry point and the other needs a second assembler.
- Role as a plugin agent, task as a preloaded skill: subagents cannot spawn agents or call Workflow, so deep research cannot run.
