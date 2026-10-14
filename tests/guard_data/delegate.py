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
    ("agent_pm.delegate", "resolve_run"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "task_index"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "trusted_dirs"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "configure"): "Legacy configuration forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "plan"): "Legacy plan forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "run"): "FR-13 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "harness_host"): "Legacy Host configuration; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "CONFIG"): "Legacy config path computed at import (os.path.join)",
    ("agent_pm.delegate", "STATUSES"): "Legacy tuple(OutcomeStatus) copy of the enum's values",
    ("agent_pm.delegate.composer", "team"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "team_dirs"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "index"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "lookup"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "path_for"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "check_all"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "catalog"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    (
        "agent_pm.delegate.composer",
        "check_team_dirs",
    ): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    (
        "agent_pm.delegate.composer",
        "check_overlap",
    ): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    (
        "agent_pm.delegate.composer",
        "team_paths",
    ): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "resolve"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    (
        "agent_pm.delegate.composer",
        "trusted_dirs",
    ): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "access"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "render"): "FR-46 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate.composer", "DELIVERABLE"): "Legacy workdir path computed at import",
    ("agent_pm.delegate.composer", "TEAM"): "Legacy team path computed at import (os.path.join)",
}


DELEGATE_SESSION_FORWARDING_ALLOWED = {
    (
        "delegate/src/agent_pm/delegate/__init__.py",
        "agent_pm.session.Driver",
    ): "FR-13 public plan()/run() forwarding and configure() for the orchestrator's engine/config.py; 356-14 (FR-27) deletes",
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
