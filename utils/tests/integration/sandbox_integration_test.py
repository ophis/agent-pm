"""Owned tmux fixture cleanup, including failed shutdown and locked allocation."""

import os
import subprocess
from pathlib import Path
from unittest import mock

from utils.tests import sandbox
from utils.tests.case import Case


class TmuxCleanup(Case):
    def test_shutdown_failures_still_remove_the_owned_directory(self):
        for error in (FileNotFoundError("tmux"), subprocess.TimeoutExpired("tmux", 10)):
            with self.subTest(error=type(error).__name__):
                root = Path(self.home, "owned")
                socket_directory = root / f"tmux-{os.getuid()}"
                socket_directory.mkdir(parents=True)
                (socket_directory / "default").touch()
                with (
                    mock.patch("pathlib.Path.is_socket", return_value=True),
                    mock.patch("subprocess.run", side_effect=error) as run,
                    self.assertRaises(type(error)),
                ):
                    sandbox._remove_tmux_directory(os.getpid(), str(root))
                run.assert_called_once()
                self.assertFalse(root.exists())

    def test_cleanup_does_not_acquire_the_allocation_lock(self):
        root = Path(self.home, "owned")
        socket_directory = root / f"tmux-{os.getuid()}"
        socket_directory.mkdir(parents=True)
        (socket_directory / "default").touch()
        with (
            sandbox._TMUX_LOCK,
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch("subprocess.run") as run,
        ):
            sandbox._remove_tmux_directory(os.getpid(), str(root))
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": os.environ["PATH"]})
        self.assertFalse(root.exists())
