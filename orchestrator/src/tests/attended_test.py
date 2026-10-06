import os, subprocess, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
import attended  # noqa: E402
import drive  # noqa: E402
import tui_claude  # noqa: E402

ID = "TASK-12"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
A, B = "engineer-TASK-12-0b6f2c1e", "dummy-tester-TASK-12-7c1d9e2f"
PANE = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3"}
ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}
NO_SERVER = (1, "no server running on /private/tmp/tmux-501/default\n")


class Tmux:
    """Fake tmux: `live` sessions answer status, list-sessions (none: no server) and kill-session, which fails for
    `fail` ones; `listing` (rc, stderr) replaces list-sessions' answer."""
    def __init__(self, live=(), fail=(), own="mine", clients="5 /dev/ttys004 %1 /tmp/s\n", listing=None):
        self.live, self.fail, self.own, self.clients, self.listing = dict.fromkeys(live), set(fail), own, clients, listing
        self.calls = []

    def __call__(self, argv, **kw):
        args = argv[1:]
        self.calls.append(args)
        out, rc, err = "", 0, ""
        if args[0] == "display-message" and args[-1] == "#{session_name}":
            out = self.own + "\n"
        elif args[0] == "list-clients":
            out = self.clients
        elif args[0] == "display-message":
            name = args[3][1:-1]  # =name:
            out = "0  \n" if name in self.live else "\n"
        elif args[0] == "list-sessions":
            rc, err = self.listing or (NO_SERVER if not self.live else (0, ""))
            out = "" if rc else "".join(n + "\n" for n in self.live)
        elif args[0] == "kill-session":
            name = args[2][1:]
            if name in self.fail:
                rc, err = 1, "boom"
            else:
                self.live.pop(name, None)
        return subprocess.CompletedProcess(argv, rc, out, err)

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

    def test_beside_given_keeps_it_with_the_opener_if_any(self):
        for environ, opener in (({}, None), (ITERM, "w0t0p0:ABC"), ({**ITERM, "ITERM_SESSION_ID": "w0t0p0:A_B"}, None),
                                (PANE, "mine")):
            with self.subTest(environ=environ):
                self.assertEqual(self.call("below", "dev", tmux=Tmux(live=["dev"]), environ=environ),
                                 drive.Layout("below", "dev", opener))

    def test_beside_not_shown(self):
        with self.assertRaisesRegex(attended.Bad, r"^no pane to show the TUI beside \(no terminal shows tmux session dev\)$"):
            self.call(beside="dev", tmux=Tmux(live=["dev"], clients=""))

    def test_inside_tmux_the_own_session_is_the_opener_never_beside(self):
        self.assertEqual(self.call(environ=PANE, tmux=Tmux(own="mine")), drive.Layout(None, None, "mine"))
        self.assertEqual(self.call("below", environ=PANE, tmux=Tmux(own="mine")), drive.Layout("below", None, "mine"))

    def test_inside_tmux_unshown_falls_back_to_iterm(self):
        got = self.call(environ={**PANE, **ITERM}, tmux=Tmux(own="mine", clients=""))
        self.assertEqual(got, drive.Layout(None, None, "mine"))

    def test_inside_tmux_unshown_no_iterm(self):
        with self.assertRaisesRegex(attended.Bad, "^no pane to show the TUI beside"):
            self.call(environ=PANE, tmux=Tmux(own="mine", clients=""))

    def test_inside_tmux_bad_pane(self):
        with self.assertRaises(attended.Bad):
            self.call(environ={"TMUX": "x", "TMUX_PANE": "junk"})

    def test_iterm_pane(self):
        tmux = Tmux()
        self.assertEqual(self.call("below", environ=ITERM, tmux=tmux), drive.Layout("below", None, "w0t0p0:ABC"))
        self.assertEqual(tmux.calls, [])

    def test_no_pane(self):
        for environ in ({}, {"ITERM_SESSION_ID": "w0t0p0"}, {"ITERM_SESSION_ID": "w0t0p0:ABC"},
                        {**ITERM, "TERM_PROGRAM": "Apple_Terminal"}):
            with self.subTest(environ=environ), self.assertRaisesRegex(
                    attended.Bad, r"^no pane to show the TUI beside \(no anchor pane: not in tmux, .*\): run from tmux "
                                  r"or iTerm2, or pass --beside SESSION$"):
                self.call(environ=environ)

    def test_a_pane_but_no_opener_without_beside(self):
        with self.assertRaisesRegex(attended.Bad, r"^no pane to show the TUI beside \(bad \$ITERM_SESSION_ID 'w0t0p0:A_B'"
                                                  r".*\): run from tmux or iTerm2, or pass --beside SESSION$"):
            self.call(environ={**ITERM, "ITERM_SESSION_ID": "w0t0p0:A_B"})


class PrefixTest(unittest.TestCase):
    def test_role_and_issue(self):
        for role in ("engineer", "dummy-tester", "pm2"):
            with self.subTest(role=role):
                self.assertEqual(attended.prefix(role, ID), f"{role}-{ID}")

    def test_a_name_outside_the_contract(self):
        for role, ident in (("Engineer", ID), ("eng_x", ID), ("", ID), ("2pm", ID), ("engineer", "task-12"),
                            ("engineer", "TASK-12 ")):
            with self.subTest(role=role, ident=ident), self.assertRaisesRegex(ValueError, "TUI session prefix"):
                attended.prefix(role, ident)


class SessionsTest(unittest.TestCase):
    OTHERS = ["engineer-TASK-1-aaaaaaaa", "Engineer-TASK-12-bbbbbbbb", "agent-pm-engineer-TASK-12",
              "agent-pm-engineer-TASK-12345678", "engineer-build-0b6f2c1e", "engineer-TASK-12-0B6F2C1E",
              "engineer-TASK-12-0b6f2c1", "engineer-task-12-0b6f2c1e", "mine"]

    def test_the_name_drive_gets_round_trips(self):
        name = drive.tui_session("engineer", "build", SID, prefix=attended.prefix("engineer", ID))
        tmux = Tmux(live=[name])
        self.assertEqual((attended.sessions(ID, proc=tmux), attended.issues(proc=tmux)), ([name], [ID]))
        self.assertEqual(tmux.calls, [["list-sessions", "-F", "#{session_name}"]] * 2)

    def test_found_by_name_only(self):
        tmux = Tmux(live=[B, *self.OTHERS, A, "pm-TASK-12-cccccccc"])
        self.assertEqual(attended.sessions(ID, proc=tmux), [B, A, "pm-TASK-12-cccccccc"])
        self.assertEqual(attended.sessions("TASK-1", proc=tmux), ["engineer-TASK-1-aaaaaaaa"])
        self.assertEqual(attended.sessions("TASK-12345678", proc=tmux), [])
        self.assertEqual(attended.issues(proc=tmux), ["TASK-1", ID])

    def test_no_server_means_none(self):
        for err in (NO_SERVER[1], "error connecting to /private/tmp/tmux-501/default (No such file or directory)\n"):
            with self.subTest(err=err):
                tmux = Tmux(listing=(1, err))
                self.assertEqual((attended.sessions(ID, proc=tmux), attended.issues(proc=tmux),
                                  attended.close(ID, proc=tmux)), ([], [], []))
                self.assertEqual(tmux.tmux_calls(), ["list-sessions"] * 3)

    def test_a_listing_failure(self):
        def oserror(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")
        for proc, want in ((Tmux(listing=(1, "error connecting to /tmp/x (Permission denied)\n")),
                            "tmux: error connecting to /tmp/x (Permission denied)"),
                           (oserror, "tmux: [Errno 2] No such file or directory: 'tmux'")):
            with self.subTest(want=want):
                for call in (lambda: attended.sessions(ID, proc=proc), lambda: attended.issues(proc=proc)):
                    with self.assertRaises(tui_claude.TuiError) as cm:
                        call()
                    self.assertEqual(str(cm.exception), want)
                self.assertEqual(attended.close(ID, proc=proc), [attended.Closed("error", None, f"TuiError: {want}")])


class CloseTest(unittest.TestCase):
    def test_kills_each_by_exact_name(self):
        tmux = Tmux(live=[A, "engineer-TASK-1-aaaaaaaa", B, "agent-pm-engineer-TASK-12"])
        self.assertEqual(attended.close(ID, proc=tmux), [attended.Closed("closed", B), attended.Closed("closed", A)])
        self.assertEqual(tmux.calls[1:], [["kill-session", "-t", f"={B}"], ["kill-session", "-t", f"={A}"]])
        self.assertEqual(list(tmux.live), ["engineer-TASK-1-aaaaaaaa", "agent-pm-engineer-TASK-12"])

    def test_a_failed_kill_is_an_error_and_the_rest_go_on(self):
        tmux = Tmux(live=[A, B], fail=[B])
        (status, name, msg), closed = attended.close(ID, proc=tmux)
        self.assertEqual(((status, name), closed), (("error", B), attended.Closed("closed", A)))
        self.assertEqual(msg, "TuiError: tmux: boom")

    def test_nothing_to_close(self):
        self.assertEqual(attended.close(ID, proc=Tmux(live=["engineer-TASK-1-aaaaaaaa"])), [])


if __name__ == "__main__":
    unittest.main()
