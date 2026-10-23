"""The utils layer's guard data (tests/guard_data/__init__.py)."""

STANDALONE = frozenset({"utils/src/agent_pm/utils/process_control.py"})


NO_COVER_ALLOWED = {
    (
        "utils/src/agent_pm/utils/process_control.py",
        "_Group._anchor",
    ): "Runs only in the forked group leader, which spawns the program and ends in os._exit.",
    (
        "utils/src/agent_pm/utils/process_control.py",
        "ProcessControl.serve",
    ): "The detached runner's control socket server: binds a Unix socket and launches the group.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "utils/tests/integration/process_control_integration_test.py",
        "a84042575bec8d8e9bdad1f86ddb6cb35b4626cf78b7a61d85c575ca8708cc54",
    ): "Existing process fixture checks requested signals wait for the programs handler even immediately after start.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "dcb5006cdf7600ff26fe3b9732496abd8279b8c10b72c81f21272ea601cd47c8",
    ): "Existing process fixture checks a missing caller expires without starting the program.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "ce5ff26113096b649ce078382cf00ba464f5ada7c2bedd34fd5deb7ba89e23ee",
    ): "Existing process fixture checks cancel a started controller ends its group and removes its directory.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "0c94261d596ced1b77dfb683829c932600cbe13a8355fd7922fe24dcbee485fe",
    ): "Existing process fixture checks natural exit retires the generation without an earlier poll.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "2eb43857d83fa84b423474a00810cc2a02471ccfba040cd5bef12bb770f8506c",
    ): "Existing process fixture checks stop allows cleanup longer than five seconds.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "b56cefd334e3df2dbb4eee94afb72e0881b56906da41d8cf3aebeb78d3d14ee4",
    ): "Existing process fixture checks the controller keeps ownership after the program leaves a descendant.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "8e3139644807bb164bd2e81e8fe63563d2f72545e2e963323e5dbd124f580c16",
    ): "Existing process fixture checks a client lost between start and started leaves no controller.",
    (
        "utils/tests/integration/process_control_integration_test.py",
        "7f09ae10ed2ccb9ee219064b27e65f9cb92df9e63a8f6cdbf230192728dff881",
    ): "Existing process fixture checks a deleted directory retires the controller once its group ends.",
    (
        "utils/tests/integration/shell_integration_test.py",
        "6fc0d68e1ac281c4aa77866e243f097ad2f56a9ec0002cdd1a5fed7ff4507390",
    ): "Existing process fixture checks exec names and shell metacharacters reach the child unchanged.",
    (
        "utils/tests/integration/shell_integration_test.py",
        "4219812d239f34d78ca09574b210a5f9424abe24b212b4929e6495f630e91bcd",
    ): "Existing process fixture checks its stdout alone run in cwd with env leading its own group.",
    (
        "utils/tests/integration/shell_integration_test.py",
        "8d0984ab264f5be96c331eef5769401afa6bf44cff514b0a037f3e98064c67ef",
    ): "Existing process fixture checks after timeout its group is killed it is reaped and the result none.",
}
