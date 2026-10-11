"""A live check, run by hand, never in CI: one headless Claude Code session through driver.api.start with the real
HOME, `claude` and config (the registry's: new/core/config.toml, then ~/.agent-pm/core-new.local.toml), tier 4, effort
low, in a temp workdir removed at the end. Prints the sid and each event as it comes, then a verdict line; a session
still running after WAIT s is stopped. Exit 0 iff SessionBound(sid), a Reply, a TurnEnd and Exited(0) came.

Imports nothing from new.tests: its package init points HOME at a temp dir, cutting `claude` off from its login."""
import os
import shutil
import sys
import tempfile
import threading

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "core", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from driver import api  # noqa: E402

PROMPT = "Reply with exactly: OK"
WAIT = 180   # seconds: the longest the session may run
STOPPED = 10   # seconds: the longest events() may take to end after stop()


def check(workdir: str) -> int:
    handle = api.start("claude", "headless", PROMPT, api.Spec(workdir=workdir, tier=4, effort="low"))
    print(f"sid {handle.sid}", flush=True)
    seen = []

    def drain():
        try:
            for event in handle.events():
                seen.append(event)
                print(repr(event), flush=True)
        except Exception as e:
            print(f"events() raised {e!r}", flush=True)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    thread.join(WAIT)
    if thread.is_alive():
        print(f"still running after {WAIT}s: stop()", flush=True)
        handle.stop()
        thread.join(STOPPED)
    missing = [name for name, found in (("SessionBound(sid)", api.SessionBound(handle.sid) in seen),
                                        ("Reply", any(isinstance(e, api.Reply) for e in seen)),
                                        ("TurnEnd", any(isinstance(e, api.TurnEnd) for e in seen)),
                                        ("Exited(0)", api.Exited(0) in seen)) if not found]
    print(f"FAIL: no {', '.join(missing)}" if missing else "PASS", flush=True)
    return 1 if missing else 0


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="agent-pm-live-")
    try:
        return check(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
