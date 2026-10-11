"""Strict fake `codex exec --json` for process tests; never reads the user's Codex account."""
import argparse
import json
import os
import select
import shlex
import sys

import fake_claude


ENV = "FAKE_CODEX_SCENARIO"


def install(bin_dir: str) -> str:
    """Put only this fake and the test interpreter on PATH for the driver and report.py."""
    for name, target in (("codex", f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"),
                         ("python3", shlex.quote(sys.executable))):
        path = os.path.join(bin_dir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec {target} "$@"\n')
        os.chmod(path, 0o755)
    return f"{bin_dir}:/usr/bin:/bin"


def parse(argv: list[str]) -> argparse.Namespace:
    if not argv or argv[0] != "exec":
        sys.exit("fake_codex.py: expected exec")
    ap = argparse.ArgumentParser(prog="codex exec", allow_abbrev=False)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sandbox")
    ap.add_argument("--skip-git-repo-check", action="store_true")
    ap.add_argument("--approve-for-me", action="store_true")
    ap.add_argument("-m", "--model")
    ap.add_argument("-c", "--config", action="append", default=[])
    ap.add_argument("--add-dir", action="append", default=[])
    ap.add_argument("prompt")
    args = ap.parse_args(argv[1:])
    if not (args.json and args.sandbox == "workspace-write" and args.skip_git_repo_check
            and args.approve_for_me and args.model):
        sys.exit("fake_codex.py: missing required headless launch settings")
    if not any(value.startswith("model_reasoning_effort=") and value.split("=", 1)[1] for value in args.config):
        sys.exit("fake_codex.py: missing model reasoning effort")
    if not args.prompt:
        sys.exit("fake_codex.py: missing prompt")
    return args


def scenario() -> dict:
    try:
        with open(os.environ[ENV]) as f:
            data = json.load(f)
    except (KeyError, OSError, ValueError) as e:
        sys.exit(f"fake_codex.py: scenario: {e}")
    if not isinstance(data, dict) or set(data) != {"steps", "log"} or not isinstance(data["steps"], list):
        sys.exit("fake_codex.py: expected steps and log")
    for step in data["steps"]:
        if isinstance(step, str):
            continue
        if not isinstance(step, dict) or step.get("kind") not in ("progress", "outcome", "stderr"):
            sys.exit("fake_codex.py: invalid step")
    return data


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main(argv: list[str]) -> int:
    args = parse(argv)
    data = scenario()
    # The driver must close stdin. A one-second timeout makes an accidental open pipe fail promptly.
    ready, _, _ = select.select([sys.stdin], [], [], 1)
    stdin_eof = bool(ready) and os.read(sys.stdin.fileno(), 1) == b""
    if not stdin_eof:
        sys.exit("fake_codex.py: stdin was not closed")
    with open(data["log"], "w") as f:
        json.dump({"argv": argv, "cwd": os.getcwd(), "prompt": args.prompt, "stdin_eof": stdin_eof}, f)
    command = fake_claude.report_command([args.prompt])
    if command is None:
        sys.exit("fake_codex.py: no report command in prompt")
    emit({"type": "thread.started", "thread_id": "fake-session"})
    emit({"type": "turn.started"})
    for step in data["steps"]:
        if isinstance(step, str):
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": step}})
        elif step["kind"] == "stderr":
            print(step["text"], file=sys.stderr, flush=True)
        else:
            fake_claude.report(command, step)
    emit({"type": "turn.completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
