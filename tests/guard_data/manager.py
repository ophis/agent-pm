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
        "manager/tests/integration/crew_engine_integration_test.py",
        "a069c336671b7322d5ba5c1a32fb4b000386686552892449f7412dbd81ea3bd1",
    ): "Existing process fixture checks start process observes starting entry unlocked events and caller path.",
    (
        "manager/tests/integration/crew_engine_integration_test.py",
        "91ba7d2d48acc90701e1a3e50996455304f3f95a0ade3105cbc09e51eca5494d",
    ): "Existing process fixture checks null or mismatched sid keep launch lost.",
    (
        "manager/tests/integration/crew_engine_integration_test.py",
        "2864c9678ab0c6627d411ebde19a8754d19e9cbf64ad381ce9626f56160fbd0b",
    ): "Existing process fixture checks final json without newline commits but truncated json stays lost.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "e20fe2f1e5d9adb32aa144fb0c2ddfc8ee7479809a740dda76d372631160e54d",
    ): "Existing process fixture checks child environment replaces inherited identity and preserves exit code.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "5a2583697fb9fdbc9d95661abd11ade09f3c96cf7b3160971f7817bb97eac04d",
    ): "Existing process fixture checks wrapper holds lock until child exits.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    ): "Existing process fixture checks forced wrapper takes over without killing old command.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "5b4b608b022cdf18f4acc381fbe749cba41a6631f1b77f3495239f5c7b317b0a",
    ): "Existing process fixture checks interrupt and terminate reach child and wait before unlock.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "b1ee562c7ce5b505e3c9868c3e68a6372c4ee8caadc2efc709af38bf831c15b7",
    ): "Existing holder fixture observes whether inherited terminal stop signals remain blocked.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "28b755cf6ae6dbe53bda7355e2f142e7348db94f5c4e134fba0d2016876c8032",
    ): "Existing process fixture checks terminate reaches manager descendants in owned process group.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "dc14a5c542c5f4702a9c2884413578ca466d54502406bf21772fbe346c5a1de2",
    ): "Existing process fixture checks terminate reaches manager descendants in owned process group.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "fa98c9a537d71c884c29d1ee96f72aad4fc54239fa28adc1c59c81c320672bea",
    ): "Existing process fixture checks terminal foreground child gets one interrupt and foreground restores.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "6d26415b6d4a9e3f34a7d14aaca0f230f2b22632427950848f371cd3d97ca1d2",
    ): "Existing process fixture checks terminal handoff failure terminates child and releases lease.",
    (
        "manager/tests/integration/rearm_integration_test.py",
        "69510745526bed812cecef6a71a7153ec6d859e2628db22b90e84c8a062dfd25",
    ): "Existing process fixture checks a sibling with the holds environment gets nothing.",
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
