"""Codex hook labels, app-server names and Driver event kinds for isolated scripts."""

HOOKS = (
    ("SessionStart", "sessionStart", "activity"),
    ("UserPromptSubmit", "userPromptSubmit", "activity"),
    ("PostToolUse", "postToolUse", "activity"),
    ("PermissionRequest", "permissionRequest", "blocked"),
    ("Stop", "stop", "turn_end"),
    ("Interrupt", "interrupt", "turn_end"),
)
