"""Tmux tile coordination behavior at standard-library boundaries."""

import contextlib
import errno
import io
import os
import re
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, suppress

from agent_pm.session.hosts.tmux import grids, tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests.unit.tmux.fixtures import TileFixture
from session.tests.unit.tmux.support import (
    PY,
    SCRIPT,
    SESSIONS,
    Tmux,
    clock,
    deadline,
    dump,
    grid_set,
    hold,
)


class TileCoordinationTest(TileFixture):
    def test_a_hook_run_returns_at_once_while_another_tile_holds_the_lock(self):
        release = hold(self, self.lock)
        fake = self.tile(*self.WIDE, wait=False)
        self.assertEqual(fake.calls, [self.SOCKET])
        release()
        fake = self.tile(*self.WIDE, wait=False)
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )

    def test_an_explicit_tile_waits_for_the_lock_then_tiles(self):
        release = hold(self, self.lock)
        fake, seen = self.fake(*self.WIDE), []

        def poll(sleeps):
            seen.append(list(fake.calls))
            if sleeps == 3:
                release()

        slept = clock(self, poll)
        tmux_host.tile("@3", proc=fake)
        self.assertEqual(slept, [tmux_model.TILE_POLL] * 3)
        self.assertEqual(seen, [[self.SOCKET]] * 3)
        self.assertEqual(
            fake.calls,
            [
                self.SOCKET,
                self.READ,
                self.PANES,
                SESSIONS,
                *self.applied(self.WIDE_SENT, str(int(time.monotonic() * 1000))),
            ],
        )

    def test_an_explicit_tile_gives_up_after_tile_wait(self):
        self.assertEqual((tmux_model.TILE_POLL, tmux_model.TILE_WAIT), (0.05, 5))
        hold(self, self.lock)
        slept = clock(self)
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_host.TuiError, "^grid @3: busy$"):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls, [self.SOCKET])
        self.assertEqual(set(slept), {tmux_model.TILE_POLL})
        self.assertTrue(5 <= sum(slept) < 5.1)  # float steps: one poll more

    def test_a_symlink_at_the_lock_path_raises_before_the_pass_and_grid_up_returns_it(self):
        os.symlink(self.sock + "-target", self.lock)
        want = f"^grid lock {re.escape(self.lock)}: {os.strerror(errno.ELOOP)}$"
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_host.TuiError, want):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls, [self.SOCKET])
        fake = self.fake(*self.WIDE)
        with (
            unittest.mock.patch.object(sys, "executable", PY),
        ):
            self.assertRegex(
                grids._grid_up(
                    "@3",
                    "%0",
                    tmux_host.GridSettings(per_column=3, retile="all"),
                    tmux_host.PaneServices(proc=fake, script=SCRIPT),
                ),
                want,
            )
        self.assertEqual(fake.calls, [grid_set("@3", "%0"), self.SOCKET])
        self.assertFalse(os.path.lexists(self.sock + "-target"))

    def test_an_empty_or_relative_socket_path_raises_before_the_lock(self):
        # the lock would land in the cwd, here an empty temp dir
        cwd = tempfile.TemporaryDirectory()
        self.addCleanup(cwd.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd.name)
        for sock in ("", "default"):
            with self.subTest(sock=sock):
                fake = self.fake(*self.WIDE, sock=sock)
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    f"^grid @3: socket path {re.escape(repr(sock))}: want an absolute path$",
                ):
                    tmux_host.tile("@3", proc=fake)
                self.assertEqual(fake.calls, [self.SOCKET])
                self.assertEqual(os.listdir(cwd.name), [])

    def test_a_fifo_at_the_lock_path_does_not_hang(self):
        os.mkfifo(self.lock, 0o600)
        with deadline(), suppress(tmux_host.TuiError):  # flock on a FIFO: ENOTSUP on macOS
            self.tile(*self.WIDE)

    def test_grid_tiles_keeps_the_re_tiles_of_the_last_tile_window_then_adds_now(self):
        # now 1000000 ms: the window is 990001..1000000; junk, stale and future times are dropped
        junk = [
            "x",
            "-999999",
            "+999999",
            "999999.0",
            "1e6",
            "\uff19\uff19\uff19",
            "0000000000999999",
            "",
        ]
        tiles = " ".join(
            [
                *["990000", "5", "0"] * 50,
                *junk,
                "990001",
                "1000001",
                "999999999999999",
                "000000000999998",
                "999999\t1000000",
            ]
        )
        fake = self.tile(*self.WIDE, tiles=tiles)
        self.assertEqual(
            fake.calls[4:], self.applied(self.WIDE_SENT, "990001 999998 999999 1000000 1000000")
        )
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], self.W3, tiles=tiles)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])

    def test_more_than_tile_limit_re_tiles_in_tile_window_tear_the_grid_down(self):
        self.assertEqual((tmux_model.TILE_LIMIT, tmux_model.TILE_WINDOW), (50, 10))
        line = "tui: grid @3: auto re-tiling stopped: 51 re-tiles in 10 s; the next worker that opens rebuilds it"
        recent = [str(990001 + i * 199) for i in range(50)]
        for message in ((0,), (1, "", "no client\n"), OSError("gone")):
            with self.subTest(message=message):
                err = io.StringIO()
                with redirect_stderr(err):
                    fake = self.tile(*self.WIDE, tiles=" ".join(recent), message=message)
                self.assertEqual(
                    fake.calls,
                    [
                        self.SOCKET,
                        self.READ,
                        self.PANES,
                        SESSIONS,
                        self.TEARDOWN,
                        ["tmux", "display-message", "-d", "10000", "-t", "@3", line],
                    ],
                )
                self.assertEqual(err.getvalue(), line + "\n")
        fake = self.tile(*self.WIDE, tiles=" ".join(recent[1:]))
        self.assertEqual(
            fake.calls[4:], self.applied(self.WIDE_SENT, " ".join([*recent[1:], "1000000"]))
        )

    def test_after_a_teardown_grid_up_under_off_sets_no_hook(self):
        recent = " ".join(str(990001 + i * 199) for i in range(50))
        with redirect_stderr(io.StringIO()):
            fake = self.tile(*self.WIDE, tiles=recent)
        self.assertEqual(fake.calls[4], self.TEARDOWN)
        del fake.calls[:]
        with (
            unittest.mock.patch.object(sys, "executable", PY),
            contextlib.nullcontext(unittest.mock.Mock()) as tile,
        ):
            self.assertIsNone(
                grids._grid_up(
                    "@3",
                    "%0",
                    tmux_host.GridSettings(per_column=3, retile="off"),
                    tmux_host.PaneServices(proc=fake, script=SCRIPT, tile_window=tile),
                )
            )
        self.assertEqual(fake.calls, [grid_set("@3", "%0", retile="off")])
        tile.assert_not_called()

    def test_bad_window_or_per_column_raises_before_tmux(self):
        cases = [
            ("3", {}, "invalid window id '3': want @[0-9]+"),
            *((window, {}, "invalid window id") for window in ("@3;", "%3", "@", " @3", "@1a")),
            ("@3", {"per_column": 0}, "per_column must be an int from 1 to 9999, not 0"),
            *(("@3", {"per_column": n}, "per_column") for n in (10000, True, "3", 2.0)),
        ]
        for window, kw, error in cases:
            with self.subTest(window=window, **kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^" + re.escape(error)):
                    tmux_host.tile(window, proc=fake, **kw)
                self.assertEqual(fake.calls, [])

    def test_a_tmux_failure_raises(self):
        # a failed re-tile: @grid-busy unset all the same, its failure not masking the re-tile's;
        # the lock freed
        fail = {"set-option": lambda argv: (1, "", "boom\n" if "@grid-tiles" in argv else "gone\n")}
        fake = self.fake(*self.WIDE, **fail)
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls[4:], self.applied(self.WIDE_SENT))
        with grids._grid_lock(self.sock, "@3", wait=False) as held:
            self.assertTrue(held)
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            self.tile(dump("160x48,0,0,0"), ["%0"], fail=("set-hook",))
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            tmux_host.tile("@3", proc=Tmux(fail=("display-message",)))
