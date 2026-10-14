"""The utils layer's guard data (tests/guard_data/__init__.py)."""

STANDALONE = frozenset({"utils/src/agent_pm/utils/process_control.py"})


PACKAGE_CLIMB_ALLOWED = {
    ("utils/src/agent_pm/utils/paths.py", "ROOT"): "TASK-393",
}
