"""Owned tmux fixture cleanup, including failed shutdown and locked allocation."""

import os
import threading
from pathlib import Path
from unittest import mock

from utils.tests import sandbox
from utils.tests.case import Case
from utils.tests.process_fixtures import TimeoutExpired


class TmuxCleanup(Case):
    def sockets(self, *names):
        root = Path(self.home, "owned")
        socket_directory = root / f"tmux-{os.getuid()}"
        socket_directory.mkdir(parents=True)
        for name in names:
            (socket_directory / name).touch()
        return root, [str(socket_directory / name) for name in names]

    def test_shutdown_failures_still_remove_the_owned_directory(self):
        for error in (FileNotFoundError("tmux"), TimeoutExpired("tmux", 10)):
            with self.subTest(error=type(error).__name__):
                root, _ = self.sockets("default")
                with (
                    mock.patch("pathlib.Path.is_socket", return_value=True),
                    mock.patch("subprocess.run", side_effect=error) as run,
                    self.assertRaises(ExceptionGroup) as raised,
                ):
                    sandbox._remove_tmux_directory(os.getpid(), str(root))
                self.assertEqual(raised.exception.exceptions, (error,))
                run.assert_called_once()
                self.assertFalse(root.exists())

    def test_a_failed_socket_does_not_stop_the_other_servers(self):
        root, sockets = self.sockets("first", "second", "third")
        failure = TimeoutExpired("tmux", 10)
        with (
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch("subprocess.run", side_effect=[failure, None, None]) as run,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            sandbox.stop_tmux_servers(str(root), {})
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["tmux", "-S", socket, "kill-server"] for socket in sockets],
        )
        self.assertEqual(raised.exception.exceptions, (failure,))

    def test_cleanup_does_not_acquire_the_allocation_lock(self):
        root, _ = self.sockets("default")
        with (
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch("subprocess.run") as run,
        ):
            with sandbox._TMUX_LOCK:
                cleanup = threading.Thread(
                    target=sandbox._remove_tmux_directory,
                    args=(os.getpid(), str(root)),
                    daemon=True,
                )
                cleanup.start()
                cleanup.join(timeout=10)
                blocked = cleanup.is_alive()
            cleanup.join()
        self.assertFalse(blocked, "tmux cleanup waited for the allocation lock")
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": os.environ["PATH"]})
        self.assertFalse(root.exists())


class ControlRoot(Case):
    def test_clean_env_carries_the_control_root_into_a_base_without_one(self):
        with mock.patch.dict(os.environ, {"AGENT_PM_CONTROL_ROOT": "/sandbox"}):
            self.assertEqual(sandbox.clean_env({})["AGENT_PM_CONTROL_ROOT"], "/sandbox")
            own = sandbox.clean_env({"AGENT_PM_CONTROL_ROOT": "/own"})
            self.assertEqual(own["AGENT_PM_CONTROL_ROOT"], "/own")
            del os.environ["AGENT_PM_CONTROL_ROOT"]
            self.assertNotIn("AGENT_PM_CONTROL_ROOT", sandbox.clean_env({}))
