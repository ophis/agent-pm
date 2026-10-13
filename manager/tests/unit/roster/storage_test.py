"""Storage behavior."""

import contextlib
import fcntl
import json
import os
import stat
import subprocess
import types
import unittest
import unittest.mock

from agent_pm.manager.engine import roster

from manager.tests.case import PY, child_env
from manager.tests.unit.roster.case import RosterCase
from manager.tests.unit.roster.data import (
    CHILD,
    MODULE,
    NOT_A_DIR,
    NOT_REGULAR,
    PRE_MOVE,
    WHEN,
    good,
    mode,
    within,
)


class StorageTest(RosterCase):
    def test_fresh_roster_is_written_0600_with_the_lock(self):
        self.addCleanup(os.umask, os.umask(0))
        with roster.locked(self.dir) as r:
            self.assertEqual(r, self.doc())
        with open(self.path) as f:
            self.assertEqual(f.read(), json.dumps(self.doc(), indent=2, sort_keys=True) + "\n")
        self.assertEqual([mode(self.path), mode(self.lock), mode(self.dir)], [0o600, 0o600, 0o700])
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])
        self.assertEqual(self.load()[0], self.doc())

    def test_changes_are_written_and_read_back(self):
        holder = {"session": "s1", "since": WHEN}
        with roster.locked(self.dir) as r:
            r.update(holder=holder, cursor=4, gen=2)
            r["entries"]["w1"] = good(note="n")
        r, err = self.load()
        self.assertEqual(
            r, self.doc(holder=holder, cursor=4, gen=2, entries={"w1": good(note="n")})
        )
        self.assertEqual(err, "")
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_unchanged_roster_is_not_rewritten(self):
        raw = self.store(json.dumps(self.doc(entries={"w1": good()}), separators=(",", ":")))
        with roster.locked(self.dir):
            pass
        self.assertEqual(self.bytes(), raw)

    def test_write_false_or_an_exception_in_the_block_writes_nothing_even_when_the_file_is_missing(
        self,
    ):
        def write_false():
            with roster.locked(self.dir, write=False) as r:
                r["cursor"] = 9

        def raising():
            with self.assertRaises(RuntimeError), roster.locked(self.dir) as r:
                r["cursor"] = 9
                raise RuntimeError

        for label, block in (("write=False", write_false), ("an exception", raising)):
            with self.subTest(label):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                block()
                self.assertFalse(os.path.exists(self.path))
                raw = self.store(self.doc())
                block()
                self.assertEqual(self.bytes(), raw)

    def test_lock_is_released_after_the_block(self):
        with self.assertRaises(RuntimeError), roster.locked(self.dir):
            raise RuntimeError
        fd = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_failed_write_removes_its_temp_file_and_keeps_the_old_file(self):
        raw = self.store(self.doc())
        with (
            unittest.mock.patch("os.replace", side_effect=OSError(5, "Input/output error")),
            self.assertRaises(roster.RosterError) as cm,
            roster.locked(self.dir) as r,
        ):
            r["cursor"] = 1
        self.assertEqual(str(cm.exception), f"{self.path}: Input/output error")
        self.assertEqual(self.bytes(), raw)
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_roster_json_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        with open(target, "w") as f:
            json.dump(self.doc(), f)
        os.symlink(target, self.path)
        self.refused(NOT_REGULAR.format(self.path))
        self.refused(NOT_REGULAR.format(self.path), write=False)
        with open(target) as f:
            self.assertEqual(json.load(f), self.doc())
        self.assertTrue(os.path.islink(self.path))

    def test_roster_json_fifo_refused_without_blocking(self):
        os.mkfifo(self.path)
        with within():
            self.refused(NOT_REGULAR.format(self.path))
            self.refused(NOT_REGULAR.format(self.path), write=False)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path).st_mode))

    def test_roster_json_directory_refused(self):
        os.mkdir(self.path)
        self.refused(NOT_REGULAR.format(self.path))

    def test_roster_json_of_another_user_refused(self):
        raw = self.store(self.doc())
        with self.foreign(self.path):
            self.refused(NOT_REGULAR.format(self.path))
        self.assertEqual(self.bytes(), raw)

    def test_oversize_roster_json_refused_and_the_limit_itself_read(self):
        raw = self.store(b"x" * (roster.ROSTER_MAX + 1))
        self.refused(f"{self.path}: over {roster.ROSTER_MAX} bytes")
        self.assertEqual(self.bytes(), raw)
        body = json.dumps(self.doc()).encode()
        self.store(body + b" " * (roster.ROSTER_MAX - len(body)))
        self.assertEqual(os.path.getsize(self.path), roster.ROSTER_MAX)
        self.assertEqual(self.load()[0], self.doc())

    def test_roster_lock_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        open(target, "w").close()
        os.symlink(target, self.lock)
        self.refused(NOT_REGULAR.format(self.lock))
        self.assertEqual(os.path.getsize(target), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_roster_lock_of_another_user_refused(self):
        open(self.lock, "w").close()
        with self.foreign(self.lock):
            self.refused(NOT_REGULAR.format(self.lock))
        self.assertFalse(os.path.exists(self.path))

    def test_busy_lock_is_refused_after_the_timeout(self):
        self.hold()
        clock = types.SimpleNamespace(now=0.0, slept=[])

        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += seconds

        with (
            unittest.mock.patch("time.monotonic", lambda: clock.now),
            unittest.mock.patch("time.sleep", sleep),
        ):
            self.refused(f"{self.lock}: busy")
        self.assertEqual(set(clock.slept), {0.1})
        self.assertAlmostEqual(sum(clock.slept), roster.LOCK_TIMEOUT, delta=0.2)
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_put_concurrently_and_both_entries_land(self):
        procs = [
            subprocess.Popen(
                [*PY, "-c", CHILD, self.dir, tag],
                env=child_env(),
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in "ab"
        ]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(set(self.entries()), {f"{t}{i}" for t in "ab" for i in range(15)})

    def test_missing_directory_is_not_attached_and_nothing_is_created(self):
        os.rmdir(self.dir)
        message = f"manager directory {self.dir} not attached: run crew.py attach first"
        self.refused(message)
        self.refused(message, write=False)
        self.assertEqual(os.listdir(self.managers), [])
        os.rmdir(self.managers)
        self.refused(message)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_directory_open_to_group_or_other_refused_not_chmodded_and_nothing_created(self):
        os.chmod(self.dir, 0o750)
        with (
            self.assertRaisesRegex(
                roster.RosterError, f"^manager directory {self.dir}: .*group or other"
            ),
            roster.locked(self.dir),
        ):
            pass
        self.assertEqual(mode(self.dir), 0o750)
        self.assertEqual(os.listdir(self.dir), [])

    def test_directory_symlink_file_and_foreign_owner_refused(self):
        refused = f"manager directory {self.dir}: {NOT_A_DIR}"
        real = os.lstat

        def lstat(path, *args, **kw):
            st = real(path, *args, **kw)
            return (
                types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
                if path == self.dir
                else st
            )

        with unittest.mock.patch("os.lstat", lstat):
            self.refused(refused)
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        os.mkdir(target, 0o700)
        os.rmdir(self.dir)
        os.symlink(target, self.dir)
        self.refused(refused)
        self.assertEqual(os.listdir(target), [])
        os.unlink(self.dir)
        open(self.dir, "w").close()
        self.refused(refused)

    def test_managers_of_another_user_or_a_symlink_refused(self):
        refused = f"manager directory {self.managers}: {NOT_A_DIR}"
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(refused)
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        os.rename(self.managers, target)
        os.symlink(target, self.managers)
        self.refused(refused)
        self.assertEqual(os.listdir(os.path.join(target, "m1")), [])

    def test_a_pre_move_crew_module_entry_is_kept_and_recovers_to_the_crew_module(self):
        self.local('runtime = "rt9"\n')
        resume = ["/usr/bin/python3", *PRE_MOVE, "start", "w1"]
        self.store(self.doc(entries={"w1": good(resume=resume)}))
        r, err = self.load()
        self.assertEqual((r["entries"], err), ({"w1": good(resume=resume)}, ""))
        self.assertEqual(
            roster.recovery(r["entries"]["w1"]),
            ["/usr/bin/python3", *MODULE, "start", "w1", "--client", "rt9"],
        )
        roster.put(self.dir, "w2", good())
        self.assertEqual(self.entries(), {"w1": good(resume=resume), "w2": good()})
