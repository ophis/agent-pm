"""The session layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SESSION = "session"


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/host.py"


TMUX_HOME = f"{SESSION_SRC}hosts/tmux/"


ADAPTERS_HOME = f"{SESSION_SRC}adapters/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


SESSION_COMMAND = "agent-pm-session"


SESSION_INTERFACE = "tests/session_interface.json"


SESSION_INTERFACE_MODULES = ("agent_pm.session", "agent_pm.session.extension")


SUBPACKAGES = {
    SESSION: {"adapters", "hosts", "runtimes"},
}


ENTRY_MODULES = {
    SESSION: {"extension.py"},
}


OBJECT_EXPORT_MODULES = (
    f"{SESSION_SRC}__init__.py",
    *(f"{SESSION_SRC}{module}" for module in sorted(ENTRY_MODULES[SESSION])),
)


OBJECT_EXPORTS_ALLOWED = {
    (f"{PKG}.{SESSION}", "read_config"): "TASK-389 layer-owned configuration entry point",
}


STANDALONE = frozenset(f"{SESSION_SRC}{path}" for path in ("adapters/codex/common/app_server.py",))


NOQA_ALLOWED = {
    (
        f"{SESSION_SRC}runtimes/headless.py",
        "BLE001",
        "HeadlessRuntime._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}runtimes/in_process.py",
        "BLE001",
        "InProcessRuntime._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}runtimes/tui_hook.py",
        "BLE001",
        "run",
    ): "the hook must never fail the client",
}


OPTION_TUPLES_ALLOWED = {
    (f"{SESSION_SRC}adapters/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}adapters/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
    (
        f"{SESSION_SRC}hosts/tmux/grid.py",
        "GRID_HOOKS",
    ): "tmux hook names the grid sets and unsets together",
}


COMMAND_SRC = (SESSION_SRC,)


NO_COVER_ALLOWED = {
    (
        f"{SESSION_SRC}hosts/pty/supervisor.py",
        "main",
    ): "The pty supervisor process: opens the pty, binds the input socket, spawns the runner.",
    (
        f"{SESSION_SRC}hosts/pty/host.py",
        "PtyTerminal._input",
    ): "Connects to the live supervisor's input socket.",
    (
        f"{SESSION_SRC}hosts/pty/host.py",
        "PtyHost._supervise",
    ): "Spawns the pty supervisor process.",
}
