"""The delegate layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

DELEGATE_SRC = f"delegate/src/{PKG}/delegate/"

CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


SUBPACKAGES = {
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    "delegate": {"config.py"},
}


OBJECT_EXPORT_MODULES = (
    "delegate/src/agent_pm/delegate/__init__.py",
    "delegate/src/agent_pm/delegate/composer/__init__.py",
    "delegate/src/agent_pm/delegate/event/__init__.py",
)


OBJECT_EXPORTS_ALLOWED = {
    ("agent_pm.delegate", "read_config"): "TASK-389 layer-owned configuration entry point",
    ("agent_pm.delegate", "module_roots"): "TASK-372 layer-owned import-root entry point",
}


OPTION_TUPLES_ALLOWED = {
    (
        f"delegate/src/{PKG}/delegate/event/outcome.py",
        "SAVES_DELIVERABLE",
    ): "the destinations whose deliverable comes back, a subset",
}


COMMAND_SRC = (f"delegate/src/{PKG}/delegate/",)


COMMAND_FILES = ("delegate/src/agent_pm/delegate/config.toml",)


COMMAND_DOCS = ("delegate/skills/", f"delegate/src/{PKG}/delegate/team/")
