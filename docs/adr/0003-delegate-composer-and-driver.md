# Delegate = composer + driver over vehicle-neutral roles and tasks

Skills, agents and `claude -p` prompts are all prompts; they differ only in when text loads, which context it enters, and the runtime envelope around it. So roles and tasks stay plain sources (`.md` text, run config in `core/config/config.toml`), not skills or agents. A composer assembles principles + role + task + input into a prompt and run config; a generic driver runs it through a client object. Supporting another client (Claude Code, a GPT client) means a new client class plus its data file, with the driver, roles and tasks unchanged.

Run access follows the driver, never the text. Config states it client-neutrally (extra dirs, commands to pre-approve); each client translates it. Which edits and commands go through is left to the client's own permission mode (Claude Code: auto mode), with no path deny rules: a deny rule can't carve an allowlist, and auto mode already reviews edits outside the working dirs. Read-only dirs are therefore a prompt request only.

## Considered Options

- Script only, or a `/delegate` skill only: each serves one entry point and the other needs a second assembler.
- Role as a plugin agent, task as a preloaded skill: subagents cannot spawn agents or call Workflow, so deep research cannot run.
