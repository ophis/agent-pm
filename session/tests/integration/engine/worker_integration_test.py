"""Worker state on the real filesystem: private modes, and a state file or pointer that is a
symlink, FIFO, directory, oversize or a link to someone else's file is refused promptly.
"""

import os
import stat
import unittest

from agent_pm.session import DriverError
from agent_pm.session.engine import worker
from agent_pm.session.engine.worker import Worker, WorkerState

from session.tests.case import Case

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class StateFileTest(Case):
    def setUp(self):
        super().setUp()
        self.state = WorkerState(name="w1", sid=SID, client="claude", cwd="/work", host="tmux")
        self.root = os.path.dirname(self.state.workdir())
        self.path = os.path.join(self.state.workdir(), worker.STATE_FILE)
        self.pointer = os.path.join(self.root, worker.BY_NAME, "w1")
        with Worker(self.state).locked():
            Worker(self.state)._save()

    def mode(self, path: str) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_its_directories_are_0700_and_its_files_0600(self):
        for path in (self.root, self.state.workdir(), os.path.dirname(self.pointer)):
            with self.subTest(path=path):
                self.assertEqual(self.mode(path), 0o700)
        for path in (self.path, self.pointer):
            with self.subTest(path=path):
                self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(Worker.load("w1").state, self.state)

    def test_a_state_file_that_is_no_regular_file_of_ours_is_refused(self):
        elsewhere = os.path.join(self.home, "elsewhere.json")
        with open(self.path, encoding="utf-8") as f, open(elsewhere, "w", encoding="utf-8") as out:
            out.write(f.read())
        for make in (
            lambda: os.symlink(elsewhere, self.path),
            lambda: os.mkfifo(self.path, 0o600),
            lambda: os.mkdir(self.path, 0o700),
            lambda: self.write(self.path, "x" * (worker.STATE_MAX + 1)),
        ):
            with self.subTest(make=make):
                self.remove(self.path)
                make()
                with self.assertRaisesRegex(DriverError, "missing, not a regular file of yours"):
                    Worker.load("w1")

    def test_a_pointer_that_is_a_symlink_or_holds_no_sid_is_refused(self):
        target = os.path.join(self.home, "sid")
        self.write(target, SID)
        self.remove(self.pointer)
        os.symlink(target, self.pointer)
        with self.assertRaisesRegex(DriverError, "^w1: not a worker"):
            Worker.load("w1")
        self.remove(self.pointer)
        self.write(self.pointer, "../../x\n")
        with self.assertRaisesRegex(DriverError, "holds no session id"):
            Worker.load("w1")

    def test_a_workers_directory_that_is_a_symlink_is_refused_before_any_lock(self):
        other = WorkerState(
            name="w2", sid=SID.replace("0", "1", 1), client="claude", cwd="/w", host="tmux"
        )
        target = os.path.join(self.home, "target")
        os.mkdir(target, 0o700)
        os.symlink(target, other.workdir())
        with (
            self.assertRaisesRegex(DriverError, "not a directory of yours"),
            Worker(other).locked(),
        ):
            self.fail("locked")
        self.assertEqual(os.listdir(target), [])

    @staticmethod
    def write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def remove(path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        elif os.path.lexists(path):
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
