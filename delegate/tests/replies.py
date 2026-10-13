"""Reply and record helpers every delegate test and its upper layers' share: an outcome block, a progress mark, a fake
client call's argv, a run record without its time, a pid's liveness."""

import json
import os


def block(data) -> str:
    return f"```agent-pm-outcome\n{json.dumps(data)}\n```"


def mark(name: str, text: str) -> str:
    return f"[agent-pm-progress:{name}] {text}"


def prompt(call: dict) -> str:
    return call["argv"][-1]


def option(call: dict, name: str) -> str | None:
    argv = call["argv"]
    return argv[argv.index(name) + 1] if name in argv else None


def bare(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "ts"}


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
