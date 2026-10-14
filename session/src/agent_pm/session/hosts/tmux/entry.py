"""Bootstrap the tmux host command from explicitly supplied package roots."""

import base64
import json
import sys


def _bootstrap(argv):
    roots = json.loads(base64.urlsafe_b64decode(argv.pop(1)))
    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]


if __name__ == "__main__":
    _bootstrap(sys.argv)

    from agent_pm.session.hosts.tmux.cli import main

    sys.exit(main())
