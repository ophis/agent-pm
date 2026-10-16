"""The manager layer's guard data (tests/guard_data/__init__.py)."""

COMMAND_DOCS = ("manager/skills/",)


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
}
