"""Session's class methods on Session itself: HostedSession's once Session.configure ran."""

import json
import subprocess
import unittest

from session.tests import live_tmux
from session.tests.case import PY, Case

UNCONFIGURED = "session not configured: call Session.configure first"
CALLED = ("start", "attach", "usage", "layout", "writable_dirs", "display", "host")
# A child: each class method on Session unconfigured, then after Session.configure; it prints
# {"unconfigured": {name: message}, "configured": {name: result or [error type, message]},
# "read_state": tui.TuiRuntime.snapshot("w1")'s error}.
DISPATCH = """import json, os
from agent_pm.session import Access, ConfigError, SessionError, Hosts, Mode, Session, Spec
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.session.runtimes import tui
home = os.environ["HOME"]
spec = Spec(workdir=home, access=Access(cwd=home), client="fake", mode=Mode.HEADLESS, prompt="hi")
calls = {
    "start": lambda: Session.start(spec),
    "attach": lambda: Session.attach("w1"),
    "list": lambda: Session.list(display=""),
    "usage": lambda: Session.usage("fake"),
    "layout": lambda: Session.layout(display="x;"),
    "writable_dirs": lambda: Session.writable_dirs(spec),
    "display": lambda: Session.display("x;"),
    "host": lambda: type(Session.host()).__name__,
}
def outcome(call):
    try:
        return call()
    except (ConfigError, SessionError) as e:
        return [type(e).__name__, str(e)]
unconfigured = {n: outcome(c)[1] for n, c in calls.items() if n != "list"}
tmux = {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"}
Session.configure(clients={}, hosts=Hosts("tmux", {"tmux": tmux}))
configured = {"configure": HostedSession._setup is not None}
configured.update({n: outcome(c) for n, c in calls.items()})
read_state = outcome(lambda: tui.TuiRuntime.snapshot("w1"))
print(json.dumps({"unconfigured": unconfigured, "configured": configured, "read_state": read_state}))
"""


class SessionClassMethods(Case):
    def test_on_session_itself_each_is_hosted_sessions_configured_by_session_configure(self):
        """In a child process, so no test configures the shared classes in-process."""
        server = live_tmux.Server(self, self.home)
        server.start("unrelated")
        res = subprocess.run(
            [*PY, "-c", DISPATCH],
            env=server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        found = json.loads(res.stdout)
        self.assertEqual(found["unconfigured"], {name: UNCONFIGURED for name in CALLED})
        self.assertEqual(found["read_state"][0], "SessionError")
        no_fake = ["ConfigError", "no [clients.fake] table in the configured clients"]
        bad_display = ["ConfigError", "display 'x;': want printable text not ending in ;"]
        self.assertEqual(
            found["configured"],
            {
                "configure": True,
                "host": "TmuxHost",
                "start": no_fake,
                "attach": found["read_state"],
                "list": [],
                "usage": no_fake,
                "layout": bad_display,
                "writable_dirs": no_fake,
                "display": bad_display,
            },
        )


if __name__ == "__main__":
    unittest.main()
