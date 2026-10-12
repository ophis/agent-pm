"""The fake runtime: runtime.py --scenario FILE --log FILE --sid SID [--resume] -- PROMPT [HOOK...]. Stdlib only.

Logs {"argv", "pid", "cwd", "env": {"PWD", "FAKE_ENV"}} as a JSON line, then runs the scenario, a JSON list of steps:
{"out": obj} prints obj as JSON, {"raw": text} prints text, {"err": text} to stderr, {"sleep": s}, {"exit": n}; then
exits 0. HOOK is logged, never run."""
import argparse
import json
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(prog="runtime.py")
    for flag in ("--scenario", "--log", "--sid"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("prompt")
    parser.add_argument("hook", nargs="*")
    args = parser.parse_args()
    with open(args.log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd(),
                            "env": {k: os.environ.get(k) for k in ("PWD", "FAKE_ENV")}}) + "\n")
    with open(args.scenario, encoding="utf-8") as f:
        steps = json.load(f)
    for step in steps:
        if "out" in step:
            print(json.dumps(step["out"]), flush=True)
        elif "raw" in step:
            print(step["raw"], flush=True)
        elif "err" in step:
            print(step["err"], file=sys.stderr, flush=True)
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "exit" in step:
            return step["exit"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
