import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

CTMUX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ctmux")
FAKE = """#!{python}
import json, os, sys
with open(os.environ["CTMUX_LOG"], "a") as f:
    f.write(json.dumps([os.getcwd()] + sys.argv[1:]) + "\\n")
sys.exit(int(os.environ["HAS_RC"]) if sys.argv[1] == "has" else 0)
"""
START = ["start-server", ";", "set", "-g", "mouse", "on", ";", "set", "-g", "history-limit", "50000",
         ";", "set", "-s", "extended-keys", "on", ";", "set", "-s", "terminal-features[99]", "xterm*:extkeys",
         ";", "new", "-d", "-s", "s1"]


class CtmuxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        bin_dir, self.home = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "a b'c$d")
        for d in (bin_dir, self.home, self.cwd):
            os.mkdir(d)
        fake = os.path.join(bin_dir, "tmux")
        with open(fake, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(fake, 0o755)
        self.log = os.path.join(self.tmp, "log")
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": self.home, "CTMUX_LOG": self.log}

    def run_ctmux(self, *args, has_rc=1):
        p = subprocess.run([CTMUX, *args], cwd=self.cwd, env={**self.env, "HAS_RC": str(has_rc)},
                           capture_output=True, text=True)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = [json.loads(line) for line in f]
        return p, calls

    def test_no_name_is_a_usage_error(self):
        p, calls = self.run_ctmux()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("usage: ctmux <session-name>", p.stderr)
        self.assertEqual(calls, [])

    def test_existing_session_only_attaches(self):
        p, calls = self.run_ctmux("s1", has_rc=0)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([c[1:] for c in calls], [["has", "-t", "=s1"], ["-CC", "attach", "-t", "=s1"]])

    def test_new_session_starts_claude_in_cwd_then_attaches(self):
        p, calls = self.run_ctmux("s1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([c[1:] for c in calls[:2]], [["has", "-t", "=s1"], START])
        self.assertEqual(calls[1][0], self.home)
        send, attach = calls[2][1:], calls[3][1:]
        self.assertEqual(send[:3] + send[4:], ["send-keys", "-t", "=s1:", "Enter"])
        self.assertEqual(shlex.split(send[3]), ["cd", self.cwd, "&&", "claude", "--permission-mode", "auto"])
        self.assertEqual(attach, ["-CC", "attach", "-t", "=s1"])
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
