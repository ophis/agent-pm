"""Tmux grid open behavior at standard-library boundaries."""

import contextlib
import io
import re
import sys
import unittest
import unittest.mock
from contextlib import redirect_stderr

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ATTACH,
    HOSTS,
    INSIDE,
    MODE_HOOKS,
    OWN,
    PGREP,
    PY,
    RETILE_ERROR,
    SCRIPT,
    SESSIONS,
    SOCK,
    WATCH,
    Tmux,
    clients_of,
    environ,
    grid_read,
    grid_set,
    mute,
    osa,
    record,
    split_window,
    unmute,
    which,
)


class GridOpenTest(Case):
    """Grid mode. The caller is in root session mgr (no @opener), pane %3, shown by a terminal
    client (tty /dev/ttys004, its pane %9); `windows` maps a pane to its window's list-panes rows
    (window, @grid-manager, pane, width, height), a pane not in it is gone; list-panes -a prints
    nothing; iTerm2 runs; a split prints %20; tile records its call in the fake's calls.
    """

    MGR = ("$1", "mgr", "", "")

    NEW = [("@1", "", "%3", "160", "48")]  # mgr's window, no grid yet

    NOTICE = "tui: show: grid: --split/--split-from ignored\n"

    def setUp(self):
        self.tile = unittest.mock.Mock(
            side_effect=lambda window, *, per_column, proc: proc.calls.append(
                ["tile", window, per_column]
            )
        )
        for p in (unittest.mock.patch.object(sys, "executable", PY),):
            p.start()
            self.addCleanup(p.stop)

    def fake(
        self, windows, rows=(MGR,), own="mgr", clients=f"5 /dev/ttys004 %9 0 {SOCK}\n", **results
    ):
        def list_panes(argv):
            if "-a" in argv:
                return 0, ""
            if argv[3] not in windows:
                return 1, "", f"can't find pane: {argv[3]}\n"
            return 0, "".join("\t".join((*r, SOCK)) + "\n" for r in windows[argv[3]])

        return Tmux(
            results={
                "display-message": (0, own + "\n"),
                "list-sessions": (0, "".join("\t".join((*r, SOCK)) + "\n" for r in rows)),
                "list-clients": (0, clients),
                "list-panes": list_panes,
                "split-window": (0, "%20\n"),
                "pgrep": (0,),
                "osascript": (0, "ok NEW\n"),
                **results,
            }
        )

    def show(self, fake, env=INSIDE, retile="all", **kw):
        """show s; retile all unless given, the mode whose hooks opened() asserts by default."""
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tmux_host.show(
                "s",
                proc=fake,
                retile=retile,
                tile_window=self.tile,
                script=kw.pop("script", SCRIPT),
                **kw,
            )

    def split(self, flag, pane, window="@1"):
        """A grid split: the window's hooks muted first, so a previous mode's hook starts no tile;
        grid_set unmutes.
        """
        return [mute(window), split_window(flag, pane)]

    def opened(self, window="@1", manager="%3", opener="mgr", n=3, retile="all", hooks=True):
        """What follows a grid split: the records, the grid's options and hooks, then tile (none
        under off).
        """
        return [
            *record(opener, "%20"),
            grid_set(window, manager, n, hooks, retile),
            *([] if retile == "off" else [["tile", window, n]]),
        ]

    def test_own_root_starts_a_grid_right_of_its_pane(self):
        # plain tmux in iTerm2 ($ITERM_SESSION_ID set, iTerm2 running): a tmux split all the same
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                grid_read("%3"),
                *self.split("-h", "%3"),
                *self.opened(),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)
        # its @pane gone: still a root
        fake = self.fake({"%3": self.NEW}, [("$1", "mgr", "", "%77")])
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%77"),
                clients_of("mgr"),
                grid_read("%3"),
                *self.split("-h", "%3"),
                *self.opened(),
            ],
        )

    def test_the_largest_pane_but_the_managers_splits_below(self):
        for workers, target in (
            ([("%10", "80", "24"), ("%11", "80", "23")], "%10"),
            ([("%12", "40", "48"), ("%8", "80", "24"), ("%4", "20", "10")], "%12"),
            ([("%8", "80", "24"), ("%12", "40", "48")], "%12"),
        ):
            with self.subTest(target=target, workers=workers):
                fake = self.fake(
                    {"%3": [("@1", "%3", "%3", "79", "48"), *(("@1", "%3", *w) for w in workers)]}
                )
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls[3:], [grid_read("%3"), *self.split("-v", target), *self.opened()]
                )

    def test_a_stored_manager_not_a_pane_of_the_window_starts_a_new_grid_at_the_anchor(self):
        for manager in ("%99", "x", "%3;", " %3"):
            with self.subTest(manager=manager):
                fake = self.fake(
                    {"%3": [("@1", manager, "%3", "79", "48"), ("@1", manager, "%5", "80", "48")]}
                )
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls[3:], [grid_read("%3"), *self.split("-v", "%5"), *self.opened()]
                )

    def test_a_root_not_the_callers_anchors_at_its_clients_pane(self):
        # the most recently active client's pane; a control-mode client (iTerm2's tmux -CC) too
        clients = f"5 /dev/ttys004 %8 0 {SOCK}\n9 /dev/ttys009 %9 1 {SOCK}\n"
        for env, head in (
            (INSIDE, [SESSIONS, clients_of("mgr"), OWN]),
            ({}, [SESSIONS, clients_of("mgr")]),
        ):
            with self.subTest(env=env):
                fake = self.fake(
                    {"%9": [("@2", "", "%9", "160", "48")]}, own="drv", clients=clients
                )
                self.assertIsNone(self.show(fake, env, opener="mgr"))
                self.assertEqual(
                    fake.calls,
                    [
                        *head,
                        grid_read("%9"),
                        *self.split("-h", "%9", "@2"),
                        *self.opened("@2", "%9"),
                    ],
                )
        # the client's pane in a grid window: that grid
        fake = self.fake(
            {"%9": [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%9", "80", "48")]}, own="drv"
        )
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(fake.calls[3:], [grid_read("%9"), *self.split("-v", "%9"), *self.opened()])

    def test_a_given_opener_that_is_the_callers_session_anchors_at_its_pane(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                clients_of("mgr"),
                OWN,
                grid_read("%3"),
                *self.split("-h", "%3"),
                *self.opened(),
            ],
        )

    def test_a_member_opens_a_sub_worker_in_its_window(self):
        rows = (self.MGR, ("$2", "w1", "mgr", "%10"))
        window = [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%10", "80", "48")]
        for kw, head in (({}, [OWN, SESSIONS]), ({"opener": "w1"}, [SESSIONS])):
            with self.subTest(**kw):
                fake = self.fake({"%10": window}, rows, own="w1")
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [*head, grid_read("%10"), *self.split("-v", "%10"), *self.opened(opener="w1")],
                )

    def test_todays_path_for_a_detached_root_or_a_member_outside_a_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                clients_of("mgr"),
                PGREP,
                osa("right", "id", "s", "ABC"),
                *record("mgr", "NEW"),
            ],
        )
        rows = (self.MGR, ("$2", "w1", "x", "%10"))
        for windows in (
            {"%10": [("@1", "", "%10", "160", "48")]},
            {"%10": [("@1", "%9", "%10", "160", "48")]},
            {},
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows, rows, own="w1", pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        grid_read("%10"),
                        clients_of("w1"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("w1", "%20"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_or_unreadable_grid_read_falls_back_to_todays_path(self):
        for windows in (
            {},
            *(
                {"%3": [row]}
                for row in (
                    ("@1;", "", "%3", "160", "48"),
                    ("1", "", "%3", "160", "48"),
                    ("@1", "", "%3;", "160", "48"),
                    ("@1", "", "%3", "x", "48"),
                    ("@1", "", "%3", "160", "²"),
                    ("@1", "", "%3", "160"),
                )
            ),
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows, pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        clients_of("mgr"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("mgr", "%20"),
                    ],
                )
        fake = self.fake({"%3": self.NEW}, **{"list-clients": (1, "", "boom\n")})
        self.assertEqual(self.show(fake), "tmux: boom" + WATCH)
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), clients_of("mgr")])

    def test_split_and_split_from_ignored_in_the_grid(self):
        for kw in ({"split": "below"}, {"split_from": "b"}, {"split": "right", "split_from": "b"}):
            with self.subTest(**kw):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        *self.split("-h", "%3"),
                        *self.opened(),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH + self.NOTICE)

    def test_split_honored_outside_the_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake, split="below"))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                clients_of("mgr"),
                PGREP,
                osa("below", "id", "s", "ABC"),
                *record("mgr", "NEW"),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_split_changes_nothing_else(self):
        # the hooks unmuted again, after a split that raises too
        fake = self.fake({"%3": self.NEW}, **{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake), "tmux: no space for new pane")
        self.assertEqual(fake.calls[3:], [grid_read("%3"), *self.split("-h", "%3"), unmute("@1")])
        self.assertEqual(
            self.stderr.getvalue(), ATTACH + "tui: show: tmux: no space for new pane\n"
        )
        fake = self.fake({}, **{"list-panes": lambda argv: (0, "@1\t\t%3\t160\t48\t/tmp/a#b\n")})
        self.assertEqual(self.show(fake), "tmux would misread '/tmp/a#b'" + WATCH)
        self.assertEqual(fake.calls[3:], [grid_read("%3"), mute("@1"), unmute("@1")])

    def test_hooks_need_paths_tmux_takes_verbatim(self):
        for target, name, bad in (
            (sys, "executable", "/a b/python3"),
            (sys, "executable", "/a#b/python3"),
            (sys, "executable", "/a/python3;"),
            (sys, "executable", "/a/pythön"),
            (sys, "executable", ""),
            (sys, "executable", None),
            (None, "script", "/a'b/tmux_host.py"),
            (None, "script", "/a$(x)/tmux_host.py"),
        ):
            with (
                self.subTest(name=name, bad=bad),
                (
                    unittest.mock.patch.object(target, name, bad)
                    if target is not None
                    else contextlib.nullcontext()
                ),
            ):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, **({"script": bad} if target is None else {})))
                self.assertEqual(
                    fake.calls[-4:],
                    [*record("mgr", "%20"), grid_set("@1", "%3", hooks=False), ["tile", "@1", 3]],
                )
                self.assertEqual(
                    self.stderr.getvalue(),
                    ATTACH + f"tui: show: grid: hooks: {bad or ''}: tmux would misread it\n",
                )

    def test_retile_modes(self):
        # None: the default, open-close
        for retile, mode in ((None, "open-close"), *((m, m) for m in ("all", "open-close", "off"))):
            with self.subTest(retile=retile):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, retile=retile))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        *self.split("-h", "%3"),
                        *self.opened(retile=mode),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_retile_modes_with_a_hook_path_tmux_would_misread(self):
        # all and open-close: the notice, no hook set, the others unset; off: no hook to set, no
        # notice
        notice = "tui: show: grid: hooks: /a b/python3: tmux would misread it\n"
        with unittest.mock.patch.object(sys, "executable", "/a b/python3"):
            for retile, printed in (("all", notice), ("open-close", notice), ("off", "")):
                with self.subTest(retile=retile):
                    fake = self.fake({"%3": self.NEW})
                    self.assertIsNone(self.show(fake, retile=retile))
                    self.assertEqual(fake.calls[6:], self.opened(retile=retile, hooks=False))
                    self.assertEqual(self.stderr.getvalue(), ATTACH + printed)

    def test_bad_retile_raises_before_tmux(self):
        for bad in ("x", True, 1, ""):
            with self.subTest(retile=bad):
                fake = self.fake({"%3": self.NEW})
                for call in (
                    lambda *, bad=bad, fake=fake: self.show(fake, retile=bad),
                    lambda *, bad=bad, fake=fake: tmux_host.open_pane("s", retile=bad, proc=fake),
                ):
                    with self.assertRaisesRegex(
                        tmux_host.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                    ):
                        call()
                self.assertEqual(fake.calls, [])

    def test_a_failure_after_the_split_is_the_shows_and_leaves_the_pane(self):
        fake = self.fake(
            {"%3": self.NEW},
            **{"set-option": lambda argv: (1, "", "boom\n") if "-w" in argv else (0,)},
        )
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.tile.side_effect = tmux_host.TuiError("tmux: boom")
        fake = self.fake({"%3": self.NEW})
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.assertNotIn("kill-session", fake.commands())
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: tmux: boom\n")

    def test_per_column(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, per_column=2))
        self.assertEqual(fake.calls[-2:], [grid_set("@1", "%3", 2), ["tile", "@1", 2]])
        for bad in (0, 10000, -1, True, "3", 2.0):
            with self.subTest(per_column=bad):
                fake = self.fake({"%3": self.NEW})
                for call in (
                    lambda *, bad=bad, fake=fake: self.show(fake, per_column=bad),
                    lambda *, bad=bad, fake=fake: tmux_host.open_pane(
                        "s", per_column=bad, proc=fake
                    ),
                ):
                    with self.assertRaisesRegex(
                        tmux_host.TuiError, "^per_column must be an int from 1 to 9999"
                    ):
                        call()
                self.assertEqual(fake.calls, [])


class RetileTest(Case):
    def test_modes(self):
        self.assertEqual(tmux_host.RETILES, ("all", "open-close", "off"))
        self.assertEqual(tmux_host.RETILE, "open-close")
        self.assertEqual(tmux_host.RETILE_HOOKS, MODE_HOOKS)
        self.assertEqual(tmux_host.GRID_PANES_SET, "@grid-panes")
        for retile in (None, *tmux_host.RETILES):
            with self.subTest(retile=retile):
                self.assertIsNone(tmux_host._retile(retile))
        for bad in ("x", True, 1, "", "ALL"):
            with (
                self.subTest(retile=bad),
                self.assertRaisesRegex(
                    tmux_host.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                ),
            ):
                tmux_host._retile(bad)
