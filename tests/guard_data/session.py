"""The session layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SESSION = "session"


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


PLUGIN_SOURCE_CONFIG = f"{SESSION_SRC}config.toml"


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/host.py"


TMUX_HOME = (
    f"{SESSION_SRC}hosts/tmux/",
    f"{SESSION_SRC}viewer/",
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
    SESSION: {"clients", "hosts", "modes", "viewer"},
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
    (f"{PKG}.{SESSION}", "get_viewer"): "TASK-616 the decided public entry to the Viewer",
}


STANDALONE = frozenset(
    f"{SESSION_SRC}{path}"
    for path in (
        "clients/codex/common/app_server.py",
        "clients/opencode/launcher.py",
        "clients/opencode/probe.py",
        "clients/opencode/transcript.py",
    )
)


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
    (
        f"{SESSION_SRC}clients/codex/tui/replies.py",
        "BLE001",
        "on_hook",
    ): "steer replies must never fail the hook or keep its done line",
    (
        f"{SESSION_SRC}clients/claude/replies.py",
        "BLE001",
        "on_hook",
    ): "replies must never fail the hook or keep its done line",
}


OPTION_TUPLES_ALLOWED = {
    (f"{SESSION_SRC}clients/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}clients/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
    (
        f"{SESSION_SRC}viewer/grid.py",
        "GRID_HOOKS",
    ): "tmux hook names the grid sets and unsets together",
}


COMMAND_SRC = (SESSION_SRC,)


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


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "session/src/agent_pm/session/hosts/docker/host.py",
        "ee61a1ec24e436f6fef0bc86511b4bfbfa91036687b7b6668107fcfbfc1bb84e",
    ): "Existing Docker prompt staging helper creates the private prompt file and writes stdin to it as the container agent user.",
    (
        "session/tests/headless_support.py",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ): "Existing headless fake client needs an immediately exiting child with no output.",
    (
        "session/tests/outside.py",
        "a181da9447afdd59e5b3220ba5b83eddd81930f1115e79f0575d7ef3a91f60f6",
    ): "Existing outside-session fixture emits its launch argument and completion marker.",
    (
        "session/tests/contracts/host.py",
        "00424bf5c82e69b5c3528316c9d071d86b8e7a4949a4c3b352d17ce62dc2c699",
    ): "Existing process fixture checks detach writes exited and the returncode when its program exits.",
    (
        "session/tests/contracts/host.py",
        "eb756730a1c8938105769a8b8733ea9e147e59243d43bd8e4b1a60ea0fe460ce",
    ): "Existing process fixture checks detach writes exited minus the signal when a signal ends its program.",
    (
        "session/tests/contracts/host.py",
        "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    ): "Existing process fixture checks a terminal writes dead when its program exits.",
    (
        "session/tests/contracts/host.py",
        "1e1c59e7f75c0509e71d1b772e31c118923b004f53d3676e760d734b56499f70",
    ): "Existing process fixture checks a paste and the enter key reach the terminal programs stdin as one line.",
    (
        "session/tests/contracts/host.py",
        "4a0492e5f41f956f07708afc3e5a791d94318ff5c44968d0fdd3477fea2d7278",
    ): "Existing process fixture checks keys in the session notation reach the terminal program translated.",
    (
        "session/tests/contracts/host.py",
        "92a1825dd7748fa5edf4c515a2a8325e6c81d6f9f3b3d5a88141b2de03d248e2",
    ): "Existing process fixture checks a foreign display is kept and listed only under itself.",
    (
        "session/tests/contracts/host.py",
        "ab0d3a903330f9ed1d9acf4303adbf105624bb7b38eb406b7dbb8b9dbeec4b8b",
    ): "Existing process fixture checks kill ends the program and its child process.",
    (
        "session/tests/contracts/host.py",
        "bcdee211cc32d0c5ba7b7ddacb450d52c6871fc7835854b7c32eb410b973cb7e",
    ): "Existing Host contract leaves a descendant alive to verify ownership and eventual group cleanup.",
    (
        "session/tests/integration/api_integration_test.py",
        "c9cc94703193e47d30046561a0a4fcd2abab8fcfdef5a75358e1f661a6b13866",
    ): "Existing API lifecycle fixtures model a grandchild retaining pipes and a child closing pipes before exit.",
    (
        "session/tests/integration/api_integration_test.py",
        "f98e8768e6f88545559b0c5ed22e1d356a0d9236bc53fa2664217fc01b0fc224",
    ): "Existing API lifecycle fixtures model a grandchild retaining pipes and a child closing pipes before exit.",
    (
        "session/tests/integration/extension_integration_test.py",
        "ce5ff26113096b649ce078382cf00ba464f5ada7c2bedd34fd5deb7ba89e23ee",
    ): "Existing process fixture checks the configured host keeps lists and ends its processes.",
    (
        "session/tests/integration/installed_integration_test.py",
        "353dd97a00934849a329050c79575964e96388802b6a50b9fa31787a61218a1e",
    ): "Existing process fixture checks the copy reads its packaged config and runs a headless session.",
    (
        "session/tests/integration/cli/cli_control_integration_test.py",
        "652ea75604247e85aac4b42f7a937530b239cc268d69bf9d91059c9f738d9135",
    ): "Existing CLI control fixture keeps a child alive while commands inspect and stop its session.",
    (
        "session/tests/integration/cli/cli_listing_integration_test.py",
        "652ea75604247e85aac4b42f7a937530b239cc268d69bf9d91059c9f738d9135",
    ): "Existing process fixture checks a session on a second host is listed with its record.",
    (
        "session/tests/integration/cli/cli_terminal_integration_test.py",
        "496219f04bbdc378c31ab9a9a53be9ff55a41da89d65fba8baedc1ece20dc298",
    ): "Existing process fixture checks a session with no terminal takes neither keys nor text.",
    (
        "session/tests/integration/clients/claude/claude_tui_integration_test.py",
        "b9b49f3529e02e5d93ca0a373221390b9b54f98dd084409d1dceb8a44590463c",
    ): "Existing process fixture checks show from inside the manager puts it in the grid close takes it out.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "fc75cc3d188bbbbe8564133ed21537010ea309a09e4e7bc4a3d7f29135815e2c",
    ): "Existing process fixture checks the module uses canonical modules dataclasses and flag type.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "7e4699b393a08c5b495eef12293120ce9612e2b3e1ddb34344a1329cd6505d0f",
    ): "Existing process fixture checks the module decodes launch data once.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "12c484c0edb7f7ebf292aa261aeca1fe260c4c668b2f4cb17d779403ad752df8",
    ): "Existing process fixture checks package imports keep sys path and share canonical modules.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "0842107b14800fd722eaff5e4ccafe1155e2741a5ec4a5fce845607c25f369af",
    ): "Existing process fixture checks a lone surrogate is one line and failure status.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "a54540807f5981fab22a3c225f83241ed92520cc5a1ea92493cd6e779e2e431f",
    ): "Existing process fixture checks shared schema preserves hooks in isolated scripts and client.",
    (
        "session/tests/integration/clients/codex/tui/start_integration_test.py",
        "57fc6aac3dc49a226570cf6f0855e21aea62e766ca50d9db62743186f3e0e6f6",
    ): "Existing process fixture checks standalone account probe uses stdlib.",
    (
        "session/tests/integration/engine/tmux_floor_integration_test.py",
        "76377c396e26a71bd5d444461b29db274083b1f75c92aa51ac6fb131664792a7",
    ): "Existing tmux floor fixture reports terminal dimensions after resize signals.",
    (
        "session/tests/integration/engine/tmux_scrollback_integration_test.py",
        "e3f53d22767e088aa44c65f48c94e58c135c9b4cd26260c0652e2411f33fcfd5",
    ): "Existing scrollback fixture emits terminal control sequences and raw input responses.",
    (
        "session/tests/integration/fakes/host_integration_test.py",
        "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    ): "Existing process fixture checks a dead name is started again.",
    (
        "session/tests/integration/fakes/host_integration_test.py",
        "715566f8d314e6ba5e585cb02f80f2503709167f6c62a987c946cdf8a3edaade",
    ): "Existing process fixture checks a retained terminal keeps its exit status and generation identity.",
    (
        "session/tests/integration/hosts/tmux/handover_integration_test.py",
        "b280f7b1f60480ae55389ae0e275c8a65962eb441702d8521b54373051b79da0",
    ): "Existing process fixture checks runs the handover.",
    (
        "session/tests/integration/hosts/tmux/handover_integration_test.py",
        "14ed8a0ba4386f8690c944c685eff82cda84e00b39ffcee757324ee9182d789e",
    ): "Existing process fixture checks any other exec failure writes einval and argv never runs.",
    (
        "session/tests/integration/hosts/tmux/hook_integration_test.py",
        "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    ): "Existing process fixture checks successful detach writes one exit event before its session is forgotten.",
    (
        "session/tests/integration/hosts/tmux/host_contract_integration_test.py",
        "d0e50accde8706182904be9cd853140355916a95ca38551ec254f56390c5f3a7",
    ): "Existing Host contract creates a separate user pane to test launched-pane ownership.",
    (
        "session/tests/integration/hosts/tmux/host_contract_integration_test.py",
        "dde8a49d4541ddce727e41605ece528a7b3ea2df40d2992193ecd0042aca312b",
    ): "Existing process fixture checks a split refocused terminal is listed and read through its launched pane.",
    (
        "session/tests/integration/hosts/tmux/host_contract_integration_test.py",
        "6899da213b7184710ff03ab2772cd4a44dff5fe6f2df4e872c99756b4ea896a3",
    ): "Existing process fixture checks killing the pane stops its program and keeps ownership of leftover children.",
    (
        "session/tests/integration/hosts/tmux/host_contract_integration_test.py",
        "beaadd9590651ff6bc7f1fb7469057844ea7334b888e135743a15dcc2599af24",
    ): "Existing process fixture checks a group that outlives its session is reclaimed by its token and killed.",
    (
        "session/tests/integration/hosts/tmux/launch_integration_test.py",
        "4b4751973a5060f8b7355c41198b4ec3d2d4a7bab7350fd346c7a5eb459a46d8",
    ): "Existing process fixture checks launch hands a long argv and env over in cwd and views are not listed.",
    (
        "session/tests/integration/hosts/tmux/launch_integration_test.py",
        "eb5c3925d7694ca785b02f131cb4c255fed6d4f298081f021eb2c840543cfd8f",
    ): "Existing process fixture checks long unicode multiline paste is literal and does not submit.",
    (
        "session/tests/integration/hosts/tmux/launch_integration_test.py",
        "1d688971ecc727a0b8051db6cffd390a19b7f4d09fe8b778461a52f72b76a5f0",
    ): "Existing process fixture checks a pane scrolled into copy mode still gets and submits pastes and keys.",
    (
        "session/tests/integration/hosts/tmux/launch_integration_test.py",
        "e39c2f670c10f449690c0698fae6b61a6afd3e8dd349f41a42d953fdc20ddeba",
    ): "Existing process fixture checks a split and refocused session is reached through its launched pane.",
    (
        "session/tests/integration/hosts/tmux/launch_integration_test.py",
        "92a1825dd7748fa5edf4c515a2a8325e6c81d6f9f3b3d5a88141b2de03d248e2",
    ): "Existing process fixture checks stale terminal cannot send to a same name replacement.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "7bd2c448755a97c00ac6675ca80403a8322dc867846058fb93c1974694f4962d",
    ): "Existing process fixture checks stdout and stderr lines become events before both pipes end.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "66da244a452d53fe838f0f297adf06dd51017eaa2155ed14f44100e33d71f12b",
    ): "Existing headless client fixture emits launch arguments as parsed JSON events.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "90f541d138e7de15306ce65ba323db16843c8ea65d28649acd87f9bb5ce1b2e1",
    ): "Existing process fixture checks ended only after stderr then stdout.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "4775c20d9a92bbb8bf8755be3db178c883a1b4a6ca14cd21e2cc91a4e29bcb88",
    ): "Existing process fixture checks an exception from parse is raised from poll.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "5744440fb00bc70b4d566e40e471f83ed12545d61ff18367e2c11d9722d35281",
    ): "Existing process fixture checks parser failure stays single and original when logging is disabled.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "ce5ff26113096b649ce078382cf00ba464f5ada7c2bedd34fd5deb7ba89e23ee",
    ): "Existing process fixture checks kills a sleeping child whose pipes then end.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "a84d6c7b6d6da79e7430eff5c0462f706df82b56488756998ca36bf3aa4b75c9",
    ): "Existing process fixture checks kills the group so a grandchild on the pipes ends them.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "5f749cdf1b3d428a28d39f0406f7b0464cf24bba02481c18d3362c89d7c80204",
    ): "Existing process fixture checks kills the group after the child exited while a grandchild holds the pipes.",
    (
        "session/tests/integration/modes/headless_integration_test.py",
        "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    ): "Existing process fixture checks skips killpg once the child is reaped and both pipes ended.",
    (
        "session/tests/integration/modes/tui_hook_integration_test.py",
        "0953936b96e2cfe4945087733719e3863df5dfd56926aa5fc129ccbb02bbb5e2",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "session/tests/integration/modes/tui_hook_integration_test.py",
        "bfe2fc0d72a89b07a9c295330864e6ddce5687bfa319892fbcdd07ae68f09340",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "session/tests/integration/modes/tui_hook_integration_test.py",
        "4523f715e13ec23a8906c522451af1d7f18e915bebfa8750f3f6b59314952703",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "session/tests/integration/modes/tui_hook_integration_test.py",
        "4338989ac7ba3b2e1bd23bf220291c4ffde5178caa41392ca098fd74ccb2731e",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "session/tests/integration/modes/tui_hook_integration_test.py",
        "43ab38dddc34c8389bc6cef67a8fb21a6733611b59ff67ad97d822a774b9fd39",
    ): "Existing process fixture checks two synchronized writers keep every complete journal record.",
    (
        "session/tests/integration/viewer/grid_integration_test.py",
        "f83f8905b4f3be6305a76adaa981467528d13853f18cc464da8221c06bf35411",
    ): "Existing process fixture checks a command ending at once gets its dead line and state.",
    (
        "session/tests/integration/viewer/grid_integration_test.py",
        "9c56e8c42a9af6de518120b69aac591acee69fd36859619fd38542a6a795cfed",
    ): "Existing process fixture checks manual and retained dead terminals keep one grid cell each.",
    (
        "session/tests/integration/viewer/show_integration_test.py",
        "90eb1f2b0c9d435d08f54643a300568dd2cab17460ad692c1f2064f0660bdc35",
    ): "Existing viewer fixture keeps a session alive while testing display placement.",
}
