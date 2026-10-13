import argparse
import json
import os
import select
import sys
import time
import uuid
from pathlib import Path


class NativeError(RuntimeError):
    @classmethod
    def expected_exec(cls):
        return cls("expected exec")

    @classmethod
    def prompt_boundary(cls):
        return cls("expected caller prompt after --")

    @classmethod
    def approval_settings(cls):
        return cls("missing approval settings")

    @classmethod
    def stdin_closed(cls):
        return cls("stdin was not closed")

    @classmethod
    def snapshot_count(cls, count):
        error = cls("expected one durable launch snapshot")
        error.count = count
        return error

    @classmethod
    def missing_binding(cls, native):
        error = cls("missing bound native session")
        error.native = native
        return error

    @classmethod
    def unknown_step(cls, step):
        error = cls("unknown fake step")
        error.step = step
        return error


NATIVE = "11111111-2222-3333-4444-555555555555"
MARKERS = (
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_VERSION",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "CODEX_PERMISSION_PROFILE",
    "CLAUDECODE",
)
DEFAULTS = {"NO_COLOR": "1", "CODEX_CI": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat"}


def parse(args):
    if not args or args[0] != "exec":
        raise NativeError.expected_exec()
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--skip-git-repo-check", action="store_true", required=True)
    parser.add_argument("-s", choices=("danger-full-access", "workspace-write"), required=True)
    parser.add_argument("-m")
    parser.add_argument("-c", action="append", default=[])
    parser.add_argument("--add-dir", action="append", default=[])
    if "--" not in args or args.index("--") != len(args) - 2:
        raise NativeError.prompt_boundary()
    prefix, prompt = args[:-2], args[-1]
    if len(prefix) >= 3 and prefix[-2] == "resume":
        native = prefix[-1]
        uuid.UUID(native)
        options = parser.parse_args(prefix[1:-2])
    else:
        native = None
        options = parser.parse_args(prefix[1:])
    if not {'approval_policy="on-request"', 'approvals_reviewer="auto_review"'} <= set(options.c):
        raise NativeError.approval_settings()
    return native, prompt


def session_path(native):
    return (
        Path(os.environ["CODEX_HOME"])
        / "sessions"
        / "2026"
        / "10"
        / "11"
        / f"rollout-fake-{native}.jsonl"
    )


def message(text):
    return {"out": {"type": "item.completed", "item": {"type": "agent_message", "text": text}}}


def main():
    args = sys.argv[1:]
    resume, prompt = parse(args)
    ready, _, _ = select.select([sys.stdin], [], [], 1)
    stdin_eof = bool(ready) and os.read(sys.stdin.fileno(), 1) == b""
    if not stdin_eof:
        raise NativeError.stdin_closed()
    records = list(
        (Path(os.environ["HOME"]) / ".agent-pm" / "core-new-sessions" / "codex").glob("*.json")
    )
    if len(records) != 1:
        raise NativeError.snapshot_count(len(records))
    state = json.loads(records[0].read_text())
    if resume and (state["native_id"] != resume or not session_path(resume).is_file()):
        raise NativeError.missing_binding(resume)
    scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())
    native = scenario.get("thread_id", resume or NATIVE)
    path = session_path(native)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": native, "cwd": os.getcwd()}})
            + "\n"
        )
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "argv": args,
                    "cwd": os.getcwd(),
                    "prompt": prompt,
                    "stdin_eof": stdin_eof,
                    "pid": os.getpid(),
                    "snapshot": state,
                    "resume_id": resume,
                    "env": {
                        key: os.environ.get(key)
                        for key in (
                            *MARKERS,
                            *DEFAULTS,
                            "CODEX_HOME",
                            "PWD",
                            "TMPDIR",
                            "NEW_POLICY_KEY",
                        )
                    },
                }
            )
            + "\n"
        )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "text": prompt}) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": native}), flush=True)
    steps = scenario.get(
        "resume" if resume and "resume" in scenario else "steps",
        [
            *(message(text) for text in ("one\n\nnext line\n", "two", "three")),
            {"out": {"type": "turn.completed", "usage": {"input_tokens": 10}}},
        ],
    )
    for step in steps:
        if "out" in step:
            print(json.dumps(step["out"]), flush=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(step["out"]) + "\n")
        elif "err" in step:
            print(step["err"], file=sys.stderr, flush=True)
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "exit" in step:
            return step["exit"]
        else:
            raise NativeError.unknown_step(step)
    return 0


if __name__ == "__main__":
    sys.exit(main())
