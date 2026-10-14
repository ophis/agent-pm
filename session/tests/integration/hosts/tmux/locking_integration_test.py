"""Real grid lockfiles reject unsafe objects and isolate each socket and window."""

import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from agent_pm.session.hosts.tmux import grid
from agent_pm.session.hosts.tmux.types import TuiError

from session.tests.case import Case


class LockingTest(Case):
    def setUp(self):
        self.socket = os.path.join(self.home, "socket")
        self.path = self.socket + "-3.lock"

    def test_lock_is_exclusive_per_socket_window_and_released_after_exit(self):
        with grid._grid_lock(self.socket, "@3", wait=False) as held:
            self.assertTrue(held)
            with grid._grid_lock(self.socket, "@3", wait=False) as other:
                self.assertFalse(other)
            with grid._grid_lock(self.socket, "@4", wait=False) as other:
                self.assertTrue(other)
            with grid._grid_lock(self.socket + "-other", "@3", wait=False) as other:
                self.assertTrue(other)
        with grid._grid_lock(self.socket, "@3", wait=False) as held:
            self.assertTrue(held)

    def test_symlink_never_follows_or_changes_its_target(self):
        with tempfile.NamedTemporaryFile(dir=self.home) as target:
            target.write(b"unchanged")
            target.flush()
            os.symlink(target.name, self.path)
            with self.assertRaises(TuiError), grid._grid_lock(self.socket, "@3", wait=False):
                self.fail("a symlink must not hold a lock")
            target.seek(0)
            self.assertEqual(target.read(), b"unchanged")

    def test_fifo_is_rejected_without_waiting_for_another_endpoint(self):
        os.mkfifo(self.path, 0o600)
        with (
            self.assertRaisesRegex(TuiError, "not a regular file owned by you"),
            grid._grid_lock(self.socket, "@3", wait=False),
        ):
            self.fail("a FIFO must not hold a lock")

    def test_foreign_owner_is_rejected_and_descriptor_released(self):
        fstat = os.fstat

        def foreign(fd):
            info = fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)

        with (
            mock.patch("os.fstat", side_effect=foreign),
            self.assertRaisesRegex(TuiError, "not a regular file owned by you"),
            grid._grid_lock(self.socket, "@3", wait=False),
        ):
            self.fail("another owner must not hold a lock")
        with grid._grid_lock(self.socket, "@3", wait=False) as held:
            self.assertTrue(held)
