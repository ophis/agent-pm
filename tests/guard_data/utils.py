"""The utils layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"


STANDALONE = frozenset({"utils/src/agent_pm/utils/process_control.py"})


PACKAGE_CLIMB_ALLOWED = {
    ("utils/src/agent_pm/utils/paths.py", "ROOT"): "TASK-393",
    ("utils/src/agent_pm/utils/paths.py", "import_roots"): "TASK-393",
    ("utils/src/agent_pm/utils/config_file.py", "read_project"): "TASK-393",
}


OPTION_TUPLES_ALLOWED = {
    (CONFIG_LEVELS, "UNCONFIGURED"): "the projects without their own config.toml, a subset",
}
