"""The Codex steer reply watcher, run as `python -P -s -m <this module> <state file>` (replies.WATCH)
by a steer's hook; never imported by its package, so runpy runs it once.
"""

import sys

from agent_pm.session.clients.codex.tui import replies


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].startswith("/"):
        print("usage: watch <absolute state file>", file=sys.stderr)
        return 2
    return replies.watch(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
