"""The session layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SESSION = "session"


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/host.py"


TMUX_HOME = f"{SESSION_SRC}hosts/tmux/"


CLIENTS_HOME = f"{SESSION_SRC}clients/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


SESSION_COMMAND = "agent-pm-session"


SESSION_INTERFACE = "tests/session_interface.json"


SESSION_INTERFACE_MODULES = ("agent_pm.session", "agent_pm.session.extension")


SUBPACKAGES = {
    SESSION: {"clients", "hosts", "runtimes"},
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


STANDALONE = frozenset(f"{SESSION_SRC}{path}" for path in ("clients/codex/common/app_server.py",))


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
    (f"{SESSION_SRC}clients/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}clients/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
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
}


PYTHON_CODE_ALLOWED = {
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "12c484c0edb7f7ebf292aa261aeca1fe260c4c668b2f4cb17d779403ad752df8",
    ): "Checks concrete Codex module identity and hook constants in a fresh interpreter.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "a54540807f5981fab22a3c225f83241ed92520cc5a1ea92493cd6e779e2e431f",
    ): "Checks concrete Codex module identity and hook constants in a fresh interpreter.",
    (
        "session/tests/integration/engine/base_integration_test.py",
        "5ee08ae1404e898c2f6edce1ceca5d068dcfbaa01d3993c8835b5c5c2435f2b1",
    ): "Checks concrete dispatcher setup and runtime snapshot errors in a clean interpreter.",
    (
        "session/tests/integration/engine/preflight_integration_test.py",
        "8fe0392fdf778e8c979f1fc66e491ab9a76efcb939eeb42ba9c8229f1a8f6674",
    ): "Loads the fake preflight client and spawn-denial targets for the side-effect check.",
    (
        "session/tests/integration/extension_tui_hook_integration_test.py",
        "8f882febd3a05f1813e9faeeb408c5560774a1f3270d22ea30ffd627975e66b5",
    ): "Loads the installed external Tui fixture to inspect its hook descriptor.",
    (
        "session/tests/integration/hosted_integration_test.py",
        "07c8a73e333aa13d8539abe7be6ff1eeaf46e1f133322ef4cb368f765b4a9208",
    ): "Checks unconfigured concrete session and TuiStore failures in a clean interpreter.",
    (
        "session/tests/integration/runtimes/tui_hook_integration_test.py",
        "e78526c2757b76e5b9d86d6c7f872aec6d7747b97733966c1e5689b076d2d3d2",
    ): "Exercises concurrent writes through the concrete hook runtime in independent processes.",
}
