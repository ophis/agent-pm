"""fake-ctl, a fake Record's ctl and resume command, run as `python3 -P ctl.py <dir> <sub> …`: status, show (exit 1:
one cannot display) and close answer for the Records on stdin (a bad line: exit 2) from <dir>/sessions.json (name →
alive, state, outcome, displayable, display; an unknown name is gone); start NAME [--display D] and resume NAME print
the Record, resume of a live one starting nothing. Each call goes to <dir>/calls.jsonl."""

import argparse
import json
import os
import shlex
import sys

from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.utils import files
from agent_pm.utils.record import Record, RecordError, Status

COMMAND = "fake-ctl"
SCRIPT = os.path.abspath(__file__)
SESSIONS = "sessions.json"
CALLS = "calls.jsonl"
GONE = {"alive": False, "state": "gone", "outcome": None, "displayable": False, "display": None}


def install(bin_dir: str, state_dir: str) -> str:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(bin_dir, COMMAND)
    argv = ("env", f"PYTHONPATH={PYTHONPATH}", sys.executable, "-P", SCRIPT)
    files.save(path, f'#!/bin/sh\nexec {shlex.join((*argv, os.path.abspath(state_dir)))} "$@"\n')
    os.chmod(path, 0o755)
    return COMMAND


def set_session(state_dir: str, name: str, **fields) -> None:
    if unknown := fields.keys() - GONE.keys():
        raise TypeError(f"set_session: unknown fields {sorted(unknown)}")
    sessions = _sessions(state_dir)
    sessions[name] = {**_session(sessions, name), **fields}
    _save(state_dir, sessions)


def calls(state_dir: str) -> list[dict]:
    try:
        with open(os.path.join(state_dir, CALLS), encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def _sessions(state_dir: str) -> dict:
    try:
        with open(os.path.join(state_dir, SESSIONS), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _session(sessions: dict, name: str) -> dict:
    return {**GONE, **sessions.get(name, {})}


def _save(state_dir: str, sessions: dict) -> None:
    files.save(os.path.join(state_dir, SESSIONS), json.dumps(sessions))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    subs = parser.add_subparsers(dest="sub", required=True)
    for sub in ("status", "show", "close"):
        subs.add_parser(sub)
    start = subs.add_parser("start")
    start.add_argument("name")
    start.add_argument("--display")
    subs.add_parser("resume").add_argument("name")
    return parser


def _start(state_dir: str, args, call: dict) -> int:
    sessions = _sessions(state_dir)
    session = _session(sessions, args.name)
    display = args.display if args.sub == "start" else session["display"]
    resume = (COMMAND, "resume", args.name)
    try:
        record = Record(
            1, "fake", args.name, (args.name,), None, None, (COMMAND,), resume, display, None
        )
    except RecordError as e:
        print(f"{COMMAND}: {e}", file=sys.stderr)
        return 2
    call["names"] = [args.name]
    if args.sub == "start" or not session["alive"]:
        live = {"alive": True, "state": "working", "outcome": None, "display": display}
        sessions[args.name] = {**session, **live}
        _save(state_dir, sessions)
    print(record.to_line())
    return 0


def _answer(state_dir: str, sub: str, call: dict) -> int:
    try:
        records = [Record.parse(line) for line in sys.stdin]
    except RecordError as e:
        print(f"{COMMAND}: stdin: {e}", file=sys.stderr)
        return 2
    call["names"] = [record.name for record in records]
    sessions = _sessions(state_dir)
    failed = 0
    for record in records:
        session = _session(sessions, record.name)
        if sub == "status":
            shown = session["alive"] and session["displayable"]
            attach = f"{COMMAND} attach {record.name}" if shown else None
            status = Status(
                record.name, session["alive"], session["state"], session["outcome"], attach
            )
            print(status.to_line())
        elif sub == "show" and not session["displayable"]:
            print(f"session {record.name}: host fake cannot display", file=sys.stderr)
            failed = 1
        elif sub == "close":
            sessions[record.name] = {**session, "alive": False, "state": "gone"}
    if sub == "close":
        _save(state_dir, sessions)
    return failed


def main(state_dir: str, *argv: str) -> int:
    call = {"argv": list(argv), "names": []}
    try:
        args = _parser().parse_args(argv)
        if args.sub in ("start", "resume"):
            return _start(state_dir, args, call)
        return _answer(state_dir, args.sub, call)
    finally:
        files.append_line(os.path.join(state_dir, CALLS), json.dumps(call) + "\n")


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
