"""Repository-wide guard data: the layers, the trees the checks scan, the unit isolation
inventory and, until each moves to its layer, the layers' data. The checks: tests/guard_checks/.
"""

from agent_pm.utils.config_file import LEVELS

UNIT_ISOLATION_ALLOWED = {}


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/host.py"


PKG = "agent_pm"


SESSION = "session"


ORCH = "linear_orchestrator"


PROJECTS = {p: p.replace("-", "_") for level in LEVELS for p in level}


LAYERS = tuple(PROJECTS)


ORCH_PKG = f"{PKG}.{ORCH}"


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "linear-orchestrator"


SUBPACKAGES = {
    SESSION: {"adapters", "hosts", "modes"},
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    SESSION: {"extension.py"},
    "delegate": {"config.py"},
}


TREES = (*PROJECTS, "tests", ".github")


TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))


CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


SESSION_INTERFACE = "tests/session_interface.json"


SESSION_INTERFACE_MODULES = ("agent_pm.session", "agent_pm.session.extension")


OBJECT_EXPORT_MODULES = (
    "delegate/src/agent_pm/delegate/__init__.py",
    "delegate/src/agent_pm/delegate/composer/__init__.py",
    "delegate/src/agent_pm/delegate/event/__init__.py",
    f"{SESSION_SRC}__init__.py",
    *(f"{SESSION_SRC}{module}" for module in sorted(ENTRY_MODULES[SESSION])),
)


OBJECT_EXPORTS_ALLOWED = {
    ("agent_pm.delegate", "read_config"): "TASK-389 layer-owned configuration entry point",
    ("agent_pm.delegate", "module_roots"): "TASK-372 layer-owned import-root entry point",
    ("agent_pm.delegate", "package_dirs"): "TASK-372 layer-owned resource-directory entry point",
    ("agent_pm.delegate", "resolve_run"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "task_index"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "trusted_dirs"): "Legacy Composer forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "configure"): "Legacy configuration forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "plan"): "Legacy plan forwarding; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "run"): "FR-13 forwarding wrapper; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "harness_host"): "Legacy Host configuration; 356-14 (FR-27) deletes",
    ("agent_pm.delegate", "CONFIG"): "Legacy config path computed at import (os.path.join)",
    ("agent_pm.delegate", "PYTHONPATH"): "Legacy import roots computed at import (os.pathsep.join)",
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
    (f"{PKG}.{SESSION}", "read_config"): "TASK-389 layer-owned configuration entry point",
    (f"{PKG}.{SESSION}", "close"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "sessions"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "note"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "session_record"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
}


IMPORT_BOOTSTRAPS = {
    f"{SESSION_SRC}adapters/codex/tui/start.py": "_bootstrap",
    f"{SESSION_SRC}hosts/tmux/entry.py": "_bootstrap",
    f"{SESSION_SRC}modes/tui_hook.py": "_bootstrap",
}


STANDALONE = frozenset({"utils/src/agent_pm/utils/process_control.py"}) | frozenset(
    f"{SESSION_SRC}{path}" for path in ("adapters/codex/common/app_server.py",)
)


PACKAGE_CLIMB_ALLOWED = {
    ("utils/src/agent_pm/utils/paths.py", "ROOT"): "TASK-393",
    ("utils/src/agent_pm/utils/paths.py", "import_roots"): "TASK-393",
    ("utils/src/agent_pm/utils/config_file.py", "read_project"): "TASK-393",
    (f"{SESSION_SRC}adapters/codex/tui/__init__.py", "import_roots()"): "TASK-393",
    ("manager/src/agent_pm/manager/engine/crew.py", "BIN"): "TASK-373",
}


DELEGATE_SESSION_FORWARDING_ALLOWED = {
    (
        "delegate/src/agent_pm/delegate/__init__.py",
        "agent_pm.session.Driver",
    ): "FR-13 public plan()/run() forwarding and configure() for the orchestrator's engine/config.py; 356-14 (FR-27) deletes",
}


ORCH_SESSION_CONTRACT = "linear_orchestrator reaches session only through delegate"


ORCH_SESSION_IMPORTS = {
    f"{ORCH_PKG}.engine.router": {"ConfigError", "DriverError"},  # until TASK-374
    f"{ORCH_PKG}.engine.run": {"ConfigError", "DriverError", "SIGNALS"},  # until TASK-374
}


NOQA_ALLOWED = {
    (
        f"{SESSION_SRC}modes/headless.py",
        "BLE001",
        "HeadlessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}modes/in_process.py",
        "BLE001",
        "InProcessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/run_comments.py",
        "BLE001",
        "_bounded.run",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.pending",
    ): "Keep reading the other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.run",
    ): "Keep processing other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "run_prune",
    ): "Prune failures leave promotions intact.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/router.py",
        "BLE001",
        "start_run",
    ): "A recorded launch may fail.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/run.py",
        "BLE001",
        "inner",
    ): "Finish reporting a failed run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "progress",
    ): "A sink failure must not stop the run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "finish",
    ): "Report the session after failed steps.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "limit",
    ): "Report the session after a failed limit.",
    (
        f"{SESSION_SRC}modes/tui_hook.py",
        "BLE001",
        "run",
    ): "the hook must never fail the client runtime",
}


ORCH_FILES = f"{ORCH_PROJECT}/{ORCH_SRC}"


OPTION_TUPLES_ALLOWED = {
    (CONFIG_LEVELS, "UNCONFIGURED"): "the projects without their own config.toml, a subset",
    (f"{SESSION_SRC}adapters/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}adapters/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
    (
        f"{SESSION_SRC}hosts/tmux/grid.py",
        "GRID_HOOKS",
    ): "tmux hook names the grid sets and unsets together",
    (
        f"delegate/src/{PKG}/delegate/event/outcome.py",
        "SAVES_DELIVERABLE",
    ): "the destinations whose deliverable comes back, a subset",
    (f"{ORCH_FILES}engine/log.py", "ONCE"): "the log fields keying a once-only line",
    (f"{ORCH_FILES}engine/config.py", "CLONES"): "checkout dir names in a workdir",
    (f"{ORCH_FILES}engine/prune.py", "ARCHIVE_ROLES"): "roles whose finished issues are archived",
}


CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


ADAPTERS_HOME = f"{SESSION_SRC}adapters/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


TMUX_HOME = f"{SESSION_SRC}hosts/tmux/"


LEGACY_HOST_NAMES = {
    (f"{SESSION_SRC}engine/forwarding.py", "TmuxHost"): (6, "TASK-375"),
    (f"{SESSION_SRC}engine/forwarding.py", "tmux"): (4, "TASK-375"),
    (f"{SESSION_SRC}engine/forwarding.py", "_opener"): (2, "TASK-375"),
    (f"{SESSION_SRC}engine/forwarding.py", "opener"): (4, "TASK-375"),
    (f"{SESSION_SRC}engine/forwarding.py", "split_from"): (4, "TASK-375"),
}


COMMAND_SRC = (
    SESSION_SRC,
    f"delegate/src/{PKG}/delegate/",
    f"{ORCH_PROJECT}/{ORCH_SRC}",
)


SESSION_COMMAND = "agent-pm-session"


CONFIG_PY = f"{ORCH_SRC}engine/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    *(f"{ORCH_SRC}engine/{name}.py" for name in ("run", "promote", "router")),
)


ONCE = (f"{ORCH_PKG}.engine.config.load", f"{ORCH_PKG}.engine.log.use_logs")


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}engine/issues.py"


COMMAND_FILES = (
    "delegate/src/agent_pm/delegate/config.toml",
    f"{ORCH_PROJECT}/{ORCH_SRC}config.toml",
)


COMMAND_DOCS = ("delegate/skills/", "manager/skills/", f"delegate/src/{PKG}/delegate/team/")
