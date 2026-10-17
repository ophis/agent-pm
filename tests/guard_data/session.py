"""The session layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SESSION = "session"


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/host.py"


TMUX_HOME = (
    f"{SESSION_SRC}hosts/tmux/",
    f"{SESSION_SRC}engine/tmux_cli.py",
    # The Docker Host drives its container's tmux: its Host and hook callback are tmux code too.
    f"{SESSION_SRC}hosts/docker/host.py",
    f"{SESSION_SRC}hosts/docker/hook.py",
)


CLIENTS_HOME = f"{SESSION_SRC}clients/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


SESSION_COMMAND = "agent-pm-session"


SESSION_INTERFACE = "tests/session_interface.json"


SESSION_INTERFACE_MODULES = ("agent_pm.session", "agent_pm.session.extension")


SUBPACKAGES = {
    SESSION: {"clients", "hosts", "modes"},
}


ENTRY_MODULES = {
    SESSION: {"extension.py"},
}


OBJECT_EXPORT_MODULES = (
    f"{SESSION_SRC}__init__.py",
    *(f"{SESSION_SRC}{module}" for module in sorted(ENTRY_MODULES[SESSION])),
)


OBJECT_EXPORTS_ALLOWED = {
    (f"{PKG}.{SESSION}", "SESSION_NAME"): "TASK-526 shared session-name validation pattern",
    (f"{PKG}.{SESSION}", "SESSION_NAME_RULE"): "TASK-526 shared session-name validation message",
}


STANDALONE = frozenset(f"{SESSION_SRC}{path}" for path in ("clients/codex/common/app_server.py",))


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
        f"{SESSION_SRC}modes/tui_hook.py",
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
        "session/tests/integration/engine/base_integration_test.py",
        "496cc309241d2277a901f9c83282e8e69274d487a82905a947bcd0e871f75c3e",
    ): "Session class-method dispatch before and after configuration requires a fresh interpreter; Hosts is imported from its defining module after its public export was removed.",
    (
        "session/tests/integration/extension_client_integration_test.py",
        "1813480e07c687e6f048d7f9359d0ed876f19c706a3ef5146952d8bbff26777b",
    ): "Loads the unexported session config reader in a fresh installed-layout interpreter to configure and launch the extension Client.",
    (
        "session/tests/integration/installed_integration_test.py",
        "353dd97a00934849a329050c79575964e96388802b6a50b9fa31787a61218a1e",
    ): "Loads the unexported session config reader from the copied installation; only a fresh interpreter proves it uses the installed package.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "12c484c0edb7f7ebf292aa261aeca1fe260c4c668b2f4cb17d779403ad752df8",
    ): "Fresh-interpreter import of codex tui submodules: sys.path untouched, canonical clients.common/codex.common identity, no _agent_pm_codex_* copies; needs internal submodules and clean sys.modules.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "a54540807f5981fab22a3c225f83241ed92520cc5a1ea92493cd6e779e2e431f",
    ): "Imports codex.tui.HOOKS under -I with only session+utils roots, as isolated hook scripts do; HOOKS is not re-exported.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "fc75cc3d188bbbbe8564133ed21537010ea309a09e4e7bc4a3d7f29135815e2c",
    ): "Runs codex tui start as __main__ under -P -s from PYTHONPATH roots (and from a cwd decoy), then checks in that interpreter that internal codex modules are canonical; needs internal submodules and a fresh interpreter.",
    (
        "session/tests/integration/extension_client_integration_test.py",
        "f38195f9a98134400a7105a83b2adbdfc69ff63513bb7024f47ea0c3cb5089f3",
    ): "Codex tui launch (argv, PYTHONPATH) from an installed-layout site through HostedSession's client registry; no public API returns a configured Client's launch, and only a fresh interpreter on the copied site sees that layout.",
    (
        "session/tests/integration/hosted_integration_test.py",
        "83b587e3233f7f777638398b45702e083168dd6725d5df94589486d2a28cbd9f",
    ): "HostedSession.list, Session.list and TuiStore.attach refused while HostedSession is unconfigured; tests may not patch HostedSession._setup (test_boundaries), and another test may have configured it in-process, so only a fresh interpreter has it unconfigured.",
    (
        "session/src/agent_pm/session/engine/tmux_cli.py",
        "2353827626a3c6e192d6841bde2f5dfdcdbbc4a8bff857d91303607d99299413",
    ): "Shipped tmux pane wrapper: stdlib-only f-string interpolating TERMINAL_KEYS (one home), run with -I; production code, no agent_pm import.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "7e4699b393a08c5b495eef12293120ce9612e2b3e1ddb34344a1329cd6505d0f",
    ): "Runs internal codex tui start (a hook entry named in argv, not a layer command) as __main__ under -P -s and counts json.loads calls in that interpreter.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "0842107b14800fd722eaff5e4ccafe1155e2741a5ec4a5fce845607c25f369af",
    ): "Runs internal codex tui start (named in argv) as __main__ under -P -s with a lone-surrogate argument to check its one-line stderr and exit status.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "57fc6aac3dc49a226570cf6f0855e21aea62e766ca50d9db62743186f3e0e6f6",
    ): "Runs the standalone account probe file by path under -I and audits that it imports only the standard library; run_path of that file is the subject.",
}
