"""A callback command for tests: `python3 -P -S callback_recorder.py LOG [--exit KIND:CODE]...
[--block KIND]`. Reads its event line from stdin, prints noise on stdout, appends one JSON line to
LOG: the stdin text, its cwd, pid, process group and blocked signals. --exit: exit CODE on an event of KIND.
--block: on an event of KIND, start a grandchild in its group, log its pid too, then sleep.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

NOISE = "callback stdout noise"
BLOCK = 600  # seconds: longer than any test waits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--exit", action="append", default=[])
    parser.add_argument("--block")
    a = parser.parse_args(argv)
    line = sys.stdin.read()
    kind = json.loads(line)["kind"]
    print(NOISE, flush=True)
    entry = {
        "line": line,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "sigmask": sorted(int(signum) for signum in signal.pthread_sigmask(signal.SIG_BLOCK, [])),
    }
    if kind == a.block:
        sleeper = (sys.executable, "-P", "-S", "-c", f"import time; time.sleep({BLOCK})")
        entry["grandchild"] = subprocess.Popen(sleeper).pid
    with open(a.log, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
    if kind == a.block:
        time.sleep(BLOCK)
    codes = dict(rule.split(":") for rule in a.exit)
    return int(codes.get(kind, 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
