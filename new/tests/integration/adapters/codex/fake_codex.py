import json
import os
import sys
from pathlib import Path

NATIVE = "11111111-2222-3333-4444-555555555555"


def main():
    records = list((Path(os.environ["HOME"]) / ".agent-pm" / "core-new-sessions" / "codex").glob("*.json"))
    if not records:
        raise RuntimeError("no durable launch snapshot")
    state = json.loads(records[0].read_text())
    args = sys.argv[1:]
    native = args[args.index("resume") + 1] if "resume" in args else NATIVE
    path = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "11" / f"rollout-fake-{native}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": native, "cwd": os.getcwd()}}) + "\n")
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": args, "cwd": os.getcwd(), "snapshot": state,
                                 "marker": os.environ.get("CODEX_THREAD_ID")}) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": native}), flush=True)
    for text in ("one\n\nnext line\n", "two", "three"):
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}), flush=True)
    if os.environ.get("FAKE_LIMIT"):
        print(json.dumps({"type": "error", "message": "You’ve hit your usage limit. Try again later."}), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}), flush=True)


if __name__ == "__main__":
    main()
