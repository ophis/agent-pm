#!/usr/bin/env python3
"""Parse Delegate commands; engine.application owns planning, execution and reporting."""

import argparse
import sys
from collections.abc import Sequence

from agent_pm.delegate.engine.application import Application, prompt
from agent_pm.session import Mode
from agent_pm.utils import Once


def main(argv: list[str], *, application=None) -> int:
    command = argv[0] if argv and argv[0] in ("prompt", "run") else "run"
    if argv and argv[0] in ("prompt", "run"):
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog=f"delegate {command}")
    ap.add_argument("--role")
    ap.add_argument("--task")
    ap.add_argument(
        "--input", metavar="TEXT|-", help="the input text, even when it names a file; -: stdin's"
    )
    ap.add_argument(
        "--out", action=Once, metavar="PATH|-", help="where the deliverable is written; -: stdout"
    )
    ap.add_argument("--workdir", action=Once)
    ap.add_argument("--repo")
    if command == "prompt":
        a = ap.parse_args(argv)
        if a.role is None:
            ap.error("the following arguments are required: --role")
        return prompt(a, application=application)
    ap.add_argument(
        "--list",
        action="store_true",
        help="list each role, its destination type and tasks, with their sources",
    )
    ap.add_argument("--runtime", action=Once, help="default: the role's runtime in the core config")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HEADLESS.value)
    ap.add_argument("--detach", action="store_true", help="run in a Driver session of its own")
    ap.add_argument("--events", metavar="FILE", help="--detach: the outcome line's file")
    ap.add_argument("--split", choices=("right", "below"))
    ap.add_argument("--split-from", metavar="SESSION")
    ap.set_defaults(harness=None, opener=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.list and (extra := _list_extra(argv)):
        ap.error(f"--list takes no other option, not {extra}")
    if not a.list and a.role is None:
        ap.error("the following arguments are required: --role")
    return (Application() if application is None else application).run_cli(a)[0]


def _list_extra(argv: Sequence[str]) -> str | None:
    return next((arg for arg in argv if not (len(arg) >= 3 and "--list".startswith(arg))), None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
