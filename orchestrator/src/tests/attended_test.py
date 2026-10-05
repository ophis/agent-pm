import os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import attended  # noqa: E402
import drive  # noqa: E402
import tui  # noqa: E402

ID = "TASK-12"
A, B = "engineer-engineering-0b6f2c1e", "engineer-engineering-7c1d9e2f"
PANE = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3"}
ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC"}


class Tmux:
    """Fake tmux: live names answer status (RUNNING) and kill; fail names make kill-session fail."""
    def __init__(self, live=(), fail=(), own="mine", clients="5 /dev/ttys004\n"):
        self.live, self.fail, self.own, self.clients, self.calls = set(live), set(fail), own, clients, []

    def __call__(self, argv, **kw):
        args = argv[1:]
        self.calls.append(args)
        out, rc = "", 0
        if args[0] == "display-message" and args[-1] == "#{session_name}":
            out = self.own + "\n"
        elif args[0] == "list-clients":
            out = self.clients
        elif args[0] == "display-message":
            name = args[3][1:-1]  # =name:
            out = "0  \n" if name in self.live else "\n"
        elif args[0] == "kill-session":
            name = args[2][1:]
            if name in self.fail:
                rc = 1
            else:
                self.live.discard(name)
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    def tmux_calls(self):
        return [c[0] for c in self.calls]


class LayoutTest(unittest.TestCase):
    def call(self, split=None, beside=None, *, tmux=None, environ=None):
        tmux = tmux or Tmux()
        with mock.patch.dict(os.environ, environ or {}, clear=True):
            return attended.layout(split, beside, proc=tmux)

    def test_bad_split(self):
        with self.assertRaisesRegex(attended.Bad, "split must be one of right, below"):
            self.call("left", environ=ITERM)

    def test_bad_beside_name(self):
        tmux = Tmux()
        with self.assertRaisesRegex(attended.Bad, "bad tmux session name"):
            self.call(beside="a:b", tmux=tmux)
        self.assertEqual(tmux.calls, [])

    def test_missing_beside_session(self):
        with self.assertRaisesRegex(attended.Bad, "no tmux session gone"):
            self.call(beside="gone", tmux=Tmux())

    def test_beside_given(self):
        got = self.call("below", "dev", tmux=Tmux(live=["dev"]))
        self.assertEqual(got, drive.Layout("below", "dev"))

    def test_inside_tmux(self):
        got = self.call(environ=PANE, tmux=Tmux(own="mine"))
        self.assertEqual(got, drive.Layout("right", "mine"))

    def test_inside_tmux_unshown_falls_back_to_iterm(self):
        got = self.call(environ={**PANE, **ITERM}, tmux=Tmux(own="mine", clients=""))
        self.assertEqual(got, drive.Layout("right", None))

    def test_inside_tmux_unshown_no_iterm(self):
        with self.assertRaisesRegex(attended.Bad, "^no pane to show the TUI beside"):
            self.call(environ=PANE, tmux=Tmux(own="mine", clients=""))

    def test_inside_tmux_bad_pane(self):
        with self.assertRaises(attended.Bad):
            self.call(environ={"TMUX": "x", "TMUX_PANE": "junk"})

    def test_iterm_pane(self):
        tmux = Tmux()
        self.assertEqual(self.call("below", environ=ITERM, tmux=tmux), drive.Layout("below", None))
        self.assertEqual(tmux.calls, [])

    def test_no_pane(self):
        for environ in ({}, {"ITERM_SESSION_ID": "w0t0p0"}):
            with self.subTest(environ=environ), self.assertRaisesRegex(
                    attended.Bad, r"^no pane to show the TUI beside: run from tmux or iTerm2, or pass --beside SESSION$"):
                self.call(environ=environ)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = self.tmp.name
        self.path = Path(self.logs, attended.RECORD_DIR, ID)

    def write(self, *lines):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(l + "\n" for l in lines))

    def close(self, tmux):
        return attended.close(ID, logs=self.logs, proc=tmux)

    def test_record_appends_and_creates_dir(self):
        attended.record(ID, A, logs=self.logs)
        attended.record(ID, B, logs=self.logs)
        self.assertEqual(self.path.read_text(), f"{A}\n{B}\n")

    def test_close_no_record(self):
        tmux = Tmux()
        self.assertEqual(self.close(tmux), [])
        self.assertEqual(tmux.calls, [])

    def test_close_gone_dropped(self):
        self.write(A)
        self.assertEqual(self.close(Tmux()), [])
        self.assertFalse(self.path.exists())

    def test_close_live_killed(self):
        self.write(A)
        tmux = Tmux(live=[A])
        self.assertEqual(self.close(tmux), [attended.Closed("closed", A)])
        self.assertNotIn(A, tmux.live)
        self.assertFalse(self.path.exists())

    def test_close_kill_error_kept(self):
        self.write(A, B)
        tmux = Tmux(live=[A, B], fail=[A])
        (status, name, msg), closed = self.close(tmux)
        self.assertEqual(closed, attended.Closed("closed", B))
        self.assertEqual((status, name), ("error", A))
        self.assertTrue(msg and "\n" not in msg)
        self.assertEqual(self.path.read_text(), f"{A}\n")

    def test_close_malformed_never_logged_nor_sent_to_tmux(self):
        bad = "evil name; $(rm -rf)"
        self.write(bad, "agent-pm-engineer-TASK-1", "task154", "")
        tmux = Tmux()
        out = self.close(tmux)
        self.assertEqual(out, [attended.Closed("skip", None, "not a TUI session name")] * 4)
        self.assertNotIn("evil", repr(out))
        self.assertEqual(tmux.calls, [])
        self.assertFalse(self.path.exists())

    def test_close_unreadable(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        (status, name, msg), = self.close(Tmux())
        self.assertEqual((status, name), ("error", None))
        self.assertTrue(msg and "\n" not in msg)

    def test_names(self):
        self.assertEqual(attended.names(ID, logs=self.logs), [])
        self.write(A, "evil name", "agent-pm-engineer-TASK-1", B, "")
        self.assertEqual(attended.names(ID, logs=self.logs), [A, B])
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(OSError):
            attended.names(ID, logs=self.logs)

    def test_recorded(self):
        self.assertEqual(attended.recorded(self.logs), [])
        self.path.parent.mkdir(parents=True)
        for name in ("TASK-9", "ABC-1", "notes", "task-1", ".tmp-x", "TASK-1.bak"):
            Path(self.path.parent, name).write_text("")
        self.assertEqual(attended.recorded(self.logs), ["ABC-1", "TASK-9"])


if __name__ == "__main__":
    unittest.main()
