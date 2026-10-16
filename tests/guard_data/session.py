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
    (f"{PKG}.{SESSION}", "SESSION_NAME"): "TASK-526 shared session-name validation pattern",
    (f"{PKG}.{SESSION}", "SESSION_NAME_RULE"): "TASK-526 shared session-name validation message",
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
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "fc75cc3d188bbbbe8564133ed21537010ea309a09e4e7bc4a3d7f29135815e2c",
    ): "Runs codex tui start as __main__ under -P -s from PYTHONPATH roots (and from a cwd decoy), then checks in that interpreter that internal codex modules are canonical; needs internal submodules and a fresh interpreter.",
    (
        "session/tests/integration/extension_client_integration_test.py",
        "4ccf3464fd781df5281df75dd49379e9f8bfbe90397bc49471d20236f53f160f",
    ): "Codex tui launch (argv, PYTHONPATH) from an installed-layout site through HostedSession's client registry; no public API returns a configured Client's launch, and only a fresh interpreter on the copied site sees that layout.",
    (
        "session/tests/integration/hosted_integration_test.py",
        "07c8a73e333aa13d8539abe7be6ff1eeaf46e1f133322ef4cb368f765b4a9208",
    ): "HostedSession.list, Session.list and TuiStore.attach refused while HostedSession is unconfigured; tests may not patch HostedSession._setup (test_boundaries), and another test may have configured it in-process, so only a fresh interpreter has it unconfigured.",
}
