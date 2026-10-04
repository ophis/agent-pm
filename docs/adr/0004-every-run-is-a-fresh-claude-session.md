# Every run is a fresh `claude -p` session

The driver always starts a new `claude -p` process, isolated from its caller, never a subagent or the caller's own session; it may resume that run's own earlier session. Research tasks must spawn agents and Workflows from their main context, and permissions (permission mode, working dirs, exact `--allowedTools`) are set per run by the driver; prompt text can only request them.

Inline runs (in the caller's own session) go through the skill client instead: the driver writes the composed role + task as a `SKILL.md`, and the caller runs it in their session, loaded in the main context, so Agent and Workflow still work. There the caller's model, effort and permissions apply; tier, effort, extra dirs and pre-approved commands do not.
