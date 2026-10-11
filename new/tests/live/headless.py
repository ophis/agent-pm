"""Real Driver headless acceptance; run directly, outside unittest and CI."""
import argparse
import json
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from driver.api import Exited, Reply, SessionBound, Spec, TurnEnd, session_info, start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="codex")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--sid", default=None)
    parser.add_argument("--tier", type=int, default=None)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    sid = args.sid or str(uuid.uuid4())
    version = subprocess.run([args.runtime, "--version"], capture_output=True, text=True,
                             check=True, timeout=5).stdout.strip()
    events, errors = [], []
    handle = start(args.runtime, "headless", "Reply exactly: TASK-318 headless acceptance. Do not use tools.",
                   Spec(workdir=str(workdir), tier=args.tier, effort=args.effort), sid=sid)

    def collect():
        try:
            events.extend(handle.events())
        except BaseException as exc:
            errors.append(str(exc))

    try:
        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        thread.join(args.timeout)
        if thread.is_alive():
            handle.stop()
            thread.join(5)
            errors.append(f"no Exited within {args.timeout}s")
        info = handle.info()
        status = session_info(args.runtime, sid, str(workdir))
        exited = events[-1] if events and isinstance(events[-1], Exited) else None
        bindings = [event.native_id for event in events if isinstance(event, SessionBound)]
        expected = "TASK-318 headless acceptance."
        passed = (not errors and exited is not None and exited.returncode == 0
                  and bindings == [info.native_id] and bool(info.native_id)
                  and any(isinstance(event, Reply) and event.text == expected for event in events)
                  and TurnEnd(expected) in events and handle.sid == sid and info.sid == sid
                  and status.resumable == "yes" and status.last_active is not None
                  and info.native_id in status.resume_command)
        print(json.dumps({"version": version, "caller_sid": sid, "native_id": info.native_id,
                          "events": [{"type": type(event).__name__, **asdict(event)} for event in events],
                          "replies": [event.text for event in events if isinstance(event, Reply)],
                          "launch_info": asdict(info), "session_info": asdict(status),
                          "errors": errors, "passed": passed},
                         ensure_ascii=False, indent=2))
        return 0 if passed else 1
    finally:
        handle.stop()


if __name__ == "__main__":
    sys.exit(main())
