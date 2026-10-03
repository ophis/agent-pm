# Every run is a fresh `claude -p` session

The driver always starts a new `claude -p` process, isolated from its caller, never a subagent or the caller's own session; it may resume that run's own earlier session. Research tasks must spawn agents and Workflows from their main context, and hard constraints (permissions, deny rules, exact `--allowedTools`) are code the driver enforces; prompt text can only request them.

An inline driver (a run in the caller's own session) is deferred, not rejected. If added, its hard constraints become prompt requests only, since a session's permissions are fixed at start; mark which constraints it cannot guarantee.
