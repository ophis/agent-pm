"""The manager layer's guard data (tests/guard_data/__init__.py)."""

COMMAND_DOCS = ("manager/skills/",)


ENTRY_MODULES = {
    "manager": {"rearm.py"},
}


STANDALONE = frozenset({"manager/src/agent_pm/manager/rearm.py"})


NO_COVER_ALLOWED = {
    (
        "manager/src/agent_pm/manager/engine/holder.py",
        "Holder.run",
    ): "Spawns the held command in its own process group, gives it the terminal, forwards signals.",
}


PYTHON_CODE_ALLOWED = {
    (
        "manager/tests/integration/events_integration_test.py",
        "fd59f72fd518af8fdd4090896e913cb7a1b0f18eaae123aa82bff7b98266f057",
    ): "Crash at each os.replace of Events.rotate/recover (agent_pm.manager.engine.events, not re-exported) needs a process killed mid-rename.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "3b219ffc99de0f3cc406f47363e7b8943d06ed62b2a19e344998a52066e85d89",
    ): "Holder.run's tty-handoff failure path needs os.tcsetpgrp patched inside the holder's own process; Holder is not re-exported.",
    (
        "manager/tests/integration/events_integration_test.py",
        "3174f044aeaf1f1adb4902ac247410e01107e76cea410e5e3fd8cfcd572bf825",
    ): "Dies (os._exit) inside the rotation lock right after Events.rotate and a durable ledger save, so recovery must finish without rollback; Events (agent_pm.manager.engine.events) is not re-exported.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "manager/tests/live_crew.py",
        "9daa6ab4c97cc1be973fdcc46409449cf75024a77d4633ac96c5fa5f35ef9279",
    ): "Existing crash fixture marks child exit and holds the shell until the test releases its gate.",
    (
        "manager/tests/integration/crew_cli_integration_test.py",
        "d15b1ac2a7a7eba11971da7b0a2ea0d5a9df3f35c995d067920091c6868b64e5",
    ): "Existing crew CLI fixture races independent appenders against the shared locked log.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "e20fe2f1e5d9adb32aa144fb0c2ddfc8ee7479809a740dda76d372631160e54d",
    ): "Existing process fixture checks child environment replaces inherited identity and preserves exit code.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "b1ee562c7ce5b505e3c9868c3e68a6372c4ee8caadc2efc709af38bf831c15b7",
    ): "Existing holder fixture observes whether inherited terminal stop signals remain blocked.",
    (
        "manager/tests/integration/receipt_integration_test.py",
        "329edc866f0b4ea0add6fb69c156a272da4eeb5b5cd36aaa98656464f4d1a8dc",
    ): "Existing process fixture checks child stdout and alias survive crew death before commit.",
    (
        "manager/tests/integration/receipt_integration_test.py",
        "612cefd7b9f6f081700e83b2ff2769dff7d72c83dff3c3414cf02c1323bb4a0a",
    ): "Existing process fixture checks child stdout and alias survive crew death before commit.",
    (
        "manager/tests/integration/roster_integration_test.py",
        "2f1a3057fb0da566cc2695eee33e45ed83fe778bd4e4406e1a8459e02ea1d06a",
    ): "Existing process fixture checks concurrent distinct process writes preserve both entries.",
}
