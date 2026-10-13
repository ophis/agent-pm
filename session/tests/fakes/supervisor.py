"""FakeHost's supervisor, run as `python3 -P supervisor.py <dir> <events or "">`, its process
group's leader: runs meta.json's argv in its cwd with env.json verbatim (a terminal's stdin: the
FIFO <dir>/input), output to <dir>/output; writes <dir>/pid, its own; when the program exits,
the event (`exited <rc>` / `dead`) to <events>, then <dir>/rc."""

import json
import os
import subprocess
import sys

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.utils import save


def main(folder: str, events: str) -> None:
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(folder, "env.json"), encoding="utf-8") as f:
        env = json.load(f)
    stdin = subprocess.DEVNULL
    if meta["kind"] == "terminal":
        os.mkfifo(os.path.join(folder, "input"), 0o600)
        stdin = os.open(os.path.join(folder, "input"), os.O_RDWR)
    with open(os.path.join(folder, "output"), "ab") as output:
        program = subprocess.Popen(
            meta["argv"], cwd=meta["cwd"], env=env, stdin=stdin, stdout=output, stderr=output
        )
    save(os.path.join(folder, "pid"), str(os.getpid()))
    rc = program.wait()
    if events:
        name = os.path.basename(folder)
        word = ("exited", rc) if meta["kind"] == "detach" else ("dead",)
        try:
            SessionStatusLog(events).write(name, *word)
        except OSError:
            pass
    save(os.path.join(folder, "rc"), str(rc))


if __name__ == "__main__":
    main(*sys.argv[1:])
