"""Tmux pane behavior at standard-library boundaries."""

import os
import subprocess
import tempfile

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.unit.tmux.fixtures import PaneFixture
from session.tests.unit.tmux.support import (
    ATTACH,
    HOSTS,
    INSIDE,
    ITERM,
    OWN,
    PGREP,
    PREFIXES,
    SESSIONS,
    TMUX,
    WATCH,
    Tmux,
    clients_of,
    osa,
    panes,
    record,
    split_window,
)


class PanePlacementTest(PaneFixture):
    def test_iterm2_pane_split_from(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "list-clients": self.clients((5, "/dev/ttys004", "%9")),
                "osascript": (0, "ok NEW\n"),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE, split_from="b", split="below"))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("b"),
                HOSTS,
                PGREP,
                osa("below", "tty", "s", "/dev/ttys004"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_split_from(self):
        clients = self.clients((5, "/dev/ttys004", "%9"), (9, "/dev/ttys005", "%7"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(fake, {}, split_from="b", split="below"))
        self.assertEqual(fake.calls[-2:], [split_window("-v", "%7"), *record(None, "%10")])
        nested = Tmux(
            results={
                "list-clients": clients,
                "list-panes": panes(("0", "/dev/ttys005", "%5")),
                "split-window": (0, "%10\n"),
            }
        )
        self.assertIsNone(self.show(nested, {}, split_from="b"))
        self.assertEqual(
            nested.calls, [clients_of("b"), HOSTS, split_window("-h", "%5"), *record(None, "%10")]
        )

    def test_control_mode_client_gets_a_tmux_split_of_own_pane(self):
        # iTerm2's tmux -CC: the control-mode client's tty is the hidden gateway tab's; iTerm2 draws
        # the tmux split
        clients = self.clients((300, "/dev/ttys009", "%1", "1"), (100, "/dev/ttys001", "%2"))
        for split, flag in ((None, "-h"), ("right", "-h"), ("below", "-v")):
            with self.subTest(split=split):
                fake = Tmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.show(fake, INSIDE, split=split))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("own"),
                        split_window(flag, "%3"),
                        *record("own", "%10"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_split_from_a_session_only_a_control_mode_client_shows(self):
        clients = self.clients((5, "/dev/ttys009", "%9", "1"))
        for env, head, opener in (({}, [], None), (INSIDE, [OWN, SESSIONS], "own")):
            with self.subTest(env=env):
                fake = Tmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.show(fake, env, split_from="b", split="below"))
                self.assertEqual(
                    fake.calls,
                    [*head, clients_of("b"), split_window("-v", "%9"), *record(opener, "%10")],
                )

    def test_control_mode_ttys_are_never_iterm2_anchors(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "osascript": (0, "ok NEW\n"),
                "list-clients": self.clients(
                    (300, "/dev/ttys003", "%2"),
                    (200, "/dev/ttys009", "%1", "1"),
                    (100, "/dev/ttys001", "%1"),
                ),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("own"),
                HOSTS,
                PGREP,
                osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys001"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_failures(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        fake = Tmux(
            results={
                "list-clients": clients,
                "pgrep": (1,),
                "split-window": (1, "", "no space for new pane\n"),
            }
        )
        self.assertEqual(self.show(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        fake = Tmux(results={"list-clients": (0, "5 /dev/ttys004 %9 0 /tmp/#s\n"), "pgrep": (1,)})
        self.assertEqual(
            self.show(fake, {}, split_from="b"), "tmux would misread '/tmp/#s'" + WATCH
        )
        self.assertNotIn("split-window", fake.commands())

    def test_split_from_not_shown_anywhere(self):
        for result in ((0, ""), (1, "", "can't find session: =b\n")):
            with self.subTest(result=result):
                fake = Tmux(results={"list-clients": result})
                self.assertTrue(self.show(fake, {}, split_from="b").endswith(WATCH))
                self.assertEqual(fake.commands(), ["list-clients"])

    def test_bad_arguments_raise(self):
        for kw in (
            {"split_from": "a b"},
            {"split_from": "=b"},
            {"split": "up"},
            {"split": "vertically"},
            {"split": ""},
            {"opener": "a b"},
            {"opener": "a:b:c"},
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tmux_host.TuiError):
                    self.show(fake, ITERM, **kw)
                self.assertEqual(fake.calls, [])

    def test_template_ignores_split_split_from_and_opener(self):
        fake = Tmux()
        self.assertIsNone(
            self.show(
                fake,
                INSIDE,
                template="open {{session}}",
                split="up",
                split_from="a b",
                opener="a b",
            )
        )
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]])

    def test_no_anchor_refuses_with_the_attach_command(self):
        for env in (
            {},
            {"ITERM_SESSION_ID": "w0t0p0"},
            {"ITERM_SESSION_ID": ""},
            {"ITERM_SESSION_ID": "w0t0p0:ABC"},
            {**ITERM, "TERM_PROGRAM": "Apple_Terminal"},
        ):
            for split in (None, "right"):
                with self.subTest(env=env, split=split):
                    fake = Tmux()
                    why = self.show(fake, env, split=split)
                    self.assertRegex(
                        why,
                        r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)"
                        + WATCH
                        + "$",
                    )
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")

    def test_the_watch_command_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                why = self.show(Tmux(), env)
                self.assertTrue(why.endswith(f"; watch it with {prefix}tmux attach -t '=s'"), why)

    def test_no_tmux(self):
        fake = Tmux()
        self.assertEqual(self.show(fake, ITERM, tmux=None), "tmux not found")
        self.assertEqual(fake.calls, [])

    def test_tmux_path_made_absolute(self):
        here = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, here)
        fake = Tmux(results={"osascript": (0, "ok NEW\n")})
        self.show(fake, ITERM, tmux="bin/tmux")
        self.assertEqual(fake.calls[2][5], os.path.join(os.getcwd(), "bin/tmux"))

    def test_iterm_session_id_failures_refuse(self):
        missing = FileNotFoundError(2, "No such file or directory", "osascript")
        for results, why in (
            ({"pgrep": (1,)}, "iTerm2 is not running"),
            ({"osascript": missing}, f"osascript: {missing}"),
            (
                {"osascript": subprocess.TimeoutExpired("osascript", 30)},
                "osascript timed out after 30 s",
            ),
            (
                {"osascript": (1, "", "execution error: boom (-1708)\n")},
                "osascript: execution error: boom (-1708)",
            ),
            (
                {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                "no iTerm2 pane shows the anchor",
            ),
        ):
            with self.subTest(why=why):
                fake = Tmux(results=results)
                self.assertEqual(self.show(fake, ITERM), why + WATCH)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}{WATCH}\n")
                self.assertNotIn("split-window", fake.commands())
                self.assertNotIn("set-option", fake.commands())

    def test_script_is_fixed_and_takes_its_values_as_arguments(self):
        script = tmux_host.APPLESCRIPT
        self.assertNotIn(TMUX, script)
        for part in (
            "on run argv",
            'if application "iTerm2" is running',
            'tell application "iTerm2"',
            "quoted form of tmuxPath",
            'quoted form of ("=" & sessionName)',
            "split vertically with default profile command",
            "split horizontally with default profile command",
            "unique id of s",
            "tty of s",
            'return "ok " & (unique id of newSession)',
        ):
            self.assertIn(part, script)
        # its panes end with their tmux session
        self.assertEqual(script.count("with default profile command paneCommand"), 2)
        for word in ("write text", "close"):
            self.assertNotIn(word, script)
