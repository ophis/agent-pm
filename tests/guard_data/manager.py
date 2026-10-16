"""The manager layer's guard data (tests/guard_data/__init__.py)."""

COMMAND_DOCS = ("manager/skills/",)


NO_COVER_ALLOWED = {
    (
        "manager/src/agent_pm/manager/engine/holder.py",
        "Holder.run",
    ): "Spawns the held command in its own process group, gives it the terminal, forwards signals.",
}
