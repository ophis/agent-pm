"""Paths behavior."""

import contextlib
import os
import re
import shutil
import stat
import types
import unittest
import unittest.mock

from agent_pm.manager.engine import roster
from agent_pm.session import DriverError
from agent_pm.utils import read_project

from manager.tests.case import REPO
from manager.tests.unit.roster.case import Base
from manager.tests.unit.roster.data import (
    NO_RUNTIME,
    NOT_A_DIR,
    NOT_REGULAR,
    SID,
    SID2,
    mode,
    within,
)


class ManagerTest(Base):
    def test_directory_is_the_manager_else_the_own_session_and_touches_nothing(self):
        asked = [("sessions", None)]
        cases = [
            ("--manager beats the own session", "m1", "own", self.dir, []),
            ("the own session", None, "own", os.path.join(self.managers, "own"), asked),
            ("no own row (outside tmux): none", None, None, None, asked),
        ]
        for label, name, own, want, calls in cases:
            with self.subTest(label):
                self.tmux.alive, self.tmux.own, self.tmux.calls = {"other"}, own, []
                self.assertEqual(roster.directory(name, driver=self.driver), want)
                self.assertEqual(self.tmux.calls, calls)
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_relative_home_gives_an_absolute_path(self):
        os.environ["HOME"] = "rel/home"
        want = os.path.join(os.path.abspath("rel/home"), ".agent-pm", "managers", "m1")
        self.assertEqual(roster.directory("m1", driver=self.driver), want)

    def test_bad_manager_names(self):
        for name in ("a/b", "..", "", "a b", "a;", "a\n"):
            for call, resolve in (
                ("directory", lambda *a: roster.directory(*a, driver=self.driver)),
                ("home", lambda n: roster.home(n, None, driver=self.driver)),
            ):
                with (
                    self.subTest(name=name, call=call),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    resolve(name)
                self.assertEqual(str(cm.exception), f"manager {name!r}: want [A-Za-z0-9_-]+")
        self.assertEqual(self.tmux.calls, [])

    def test_unknown_sessions_name_the_manager_option(self):
        self.tmux.error = DriverError("tmux sessions unknown: bad $TMUX_PANE '%3;'")
        for call, resolve in (
            ("directory", lambda *a: roster.directory(*a, driver=self.driver)),
            ("home", lambda: roster.home(None, None, driver=self.driver)),
        ):
            with self.subTest(call), self.assertRaises(roster.RosterError) as cm:
                resolve()
            self.assertEqual(
                str(cm.exception),
                "tmux sessions unknown: bad $TMUX_PANE '%3;'; give --manager <name>",
            )

    def test_home_is_the_directory_whose_events_file_is_given(self):
        own = os.path.join(self.managers, "own")
        self.tmux.own = "own"
        for label, name, d, calls in (
            ("--manager", "m1", self.dir, 0),
            ("the own session", None, own, 1),
        ):
            with self.subTest(label):
                roster.ensure(d)
                self.tmux.calls = []
                self.assertEqual(
                    roster.home(name, os.path.join(d, "events"), driver=self.driver), d
                )
                self.assertEqual(len(self.tmux.calls), calls)
                self.assertEqual(os.listdir(d), [])

    def test_home_is_none_without_the_directory_or_its_events_file(self):
        roster.ensure(self.dir)
        events = os.path.join(self.dir, "events")
        cases = {
            "events another file": ("m1", os.path.join(self.outside("x"), "events")),
            "events a sibling file": ("m1", os.path.join(self.dir, "roster.json")),
            "events None": ("m1", None),
            "directory missing": ("m2", os.path.join(self.managers, "m2", "events")),
            "no own row, no name": (None, events),
        }
        for why, (name, given) in cases.items():
            with self.subTest(why):
                self.assertIsNone(roster.home(name, given, driver=self.driver))
        self.assertEqual(os.listdir(self.managers), ["m1"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_home_compares_real_paths(self):
        roster.ensure(self.dir)
        base = os.path.dirname(self.agent_pm)
        os.symlink(self.dir, os.path.join(base, "link"))
        self.assertEqual(
            roster.home("m1", os.path.join(base, "link", "events"), driver=self.driver), self.dir
        )
        os.symlink(base, os.path.join(base, "alias"))
        os.environ["HOME"] = os.path.join(base, "alias")
        self.assertEqual(
            roster.home("m1", os.path.join(self.dir, "events"), driver=self.driver),
            os.path.join(base, "alias", ".agent-pm", "managers", "m1"),
        )

    def test_events_path_without_create_touches_nothing(self):
        self.assertEqual(roster.events(self.dir, create=False), os.path.join(self.dir, "events"))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_created_0700_with_a_0600_events_whatever_the_umask(self):
        for mask in (0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                shutil.rmtree(self.agent_pm)
                self.umask(mask)
                path = roster.events(self.dir)
                self.assertEqual(path, os.path.join(self.dir, "events"))
                for made in (self.agent_pm, self.managers, self.dir):
                    self.assertEqual(mode(made), 0o700, made)
                self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
                self.assertEqual(mode(path), 0o600)

    def test_directories_are_0700_even_when_the_umask_strips_owner_bits(self):
        self.umask(0o277)
        roster.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o700, 0o700])

    def test_existing_managers_directory_keeps_its_mode(self):
        os.mkdir(self.managers)
        os.chmod(self.managers, 0o755)
        roster.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o755, 0o700])

    def test_existing_directory_is_kept(self):
        roster.ensure(self.dir)
        with open(roster.events(self.dir), "w") as f:
            f.write("12:00:00 w1 done\n")
        roster.ensure(self.dir)
        roster.events(self.dir)
        self.assertEqual(mode(self.dir), 0o700)
        with open(os.path.join(self.dir, "events")) as f:
            self.assertEqual(f.read(), "12:00:00 w1 done\n")

    def test_directory_open_to_group_or_other_refused_and_not_chmodded(self):
        roster.ensure(self.dir)
        for bits in (0o750, 0o705, 0o770, 0o777, 0o701):
            os.chmod(self.dir, bits)
            for call in (roster.ensure, roster.events):
                with (
                    self.subTest(bits=oct(bits), call=call.__name__),
                    self.assertRaisesRegex(
                        roster.RosterError, f"^manager directory {self.dir}: .*group or other"
                    ),
                ):
                    call(self.dir)
            self.assertEqual(mode(self.dir), bits)
            self.assertEqual(os.listdir(self.dir), [])

    def refused(self, path):
        with self.assertRaises(roster.RosterError) as cm:
            roster.events(self.dir)
        self.assertEqual(str(cm.exception), f"manager directory {path}: {NOT_A_DIR}")

    def test_managers_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.symlink(target, self.managers)
        self.refused(self.managers)
        self.assertEqual(os.listdir(target), [])

    def test_name_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.mkdir(self.managers)
        os.symlink(target, self.dir)
        self.refused(self.dir)
        self.assertEqual(os.listdir(target), [])

    def test_name_file_refused(self):
        os.mkdir(self.managers)
        open(self.dir, "w").close()
        self.refused(self.dir)

    def test_directory_owned_by_another_user_refused(self):
        os.mkdir(self.managers)
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(self.managers)

    def test_agent_pm_a_file_refused(self):
        os.rmdir(self.agent_pm)
        open(self.agent_pm, "w").close()
        with self.assertRaisesRegex(roster.RosterError, f"^manager directory {self.agent_pm}: "):
            roster.ensure(self.dir)

    def test_events_not_a_regular_file_of_yours_refused_without_blocking(self):
        events = os.path.join(self.dir, "events")
        target = os.path.join(self.outside("target"), "file")
        real = os.fstat

        def foreign(fd):
            st = real(fd)
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)

        cases = [
            ("a symlink", lambda: os.symlink(target, events), None, NOT_REGULAR.format(events)),
            ("a fifo", lambda: os.mkfifo(events), None, None),
            ("a directory", lambda: os.mkdir(events), None, None),
            (
                "another user's",
                lambda: open(events, "w").close(),
                foreign,
                NOT_REGULAR.format(events),
            ),
        ]
        for label, make, fstat, message in cases:
            with self.subTest(label):
                roster.ensure(self.dir)
                with open(target, "w"):
                    pass
                make()
                with (
                    unittest.mock.patch("os.fstat", fstat) if fstat else contextlib.nullcontext(),
                    within(),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    roster.events(self.dir)
                if message is None:
                    self.assertRegex(str(cm.exception), f"^{re.escape(events)}: ")
                else:
                    self.assertEqual(str(cm.exception), message)
                self.assertEqual(os.path.getsize(target), 0)
                shutil.rmtree(self.dir)


class WorkerDirTest(Base):
    def setUp(self):
        super().setUp()
        self.workers = os.path.join(self.agent_pm, "workers")
        self.path = os.path.join(self.workers, SID)

    def refused(self, path, why=NOT_A_DIR):
        with self.assertRaises(roster.RosterError) as cm:
            roster.worker_dir(SID)
        self.assertEqual(str(cm.exception), f"worker directory {path}: {why}")

    def test_made_level_by_level_0700_whatever_the_umask(self):
        for mask in (0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                shutil.rmtree(self.agent_pm)
                self.umask(mask)
                self.assertEqual(roster.worker_dir(SID), self.path)
                for made in (self.agent_pm, self.workers, self.path):
                    self.assertEqual(mode(made), 0o700, made)
                self.assertEqual(os.listdir(self.path), [])
        self.umask(0o277)
        self.assertEqual(roster.worker_dir(SID2), os.path.join(self.workers, SID2))
        self.assertEqual(mode(os.path.join(self.workers, SID2)), 0o700)

    def test_relative_home_gives_an_absolute_path(self):
        base = os.path.dirname(self.agent_pm)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(base)
        os.environ["HOME"] = "."
        self.assertEqual(
            roster.worker_dir(SID), os.path.join(os.getcwd(), ".agent-pm", "workers", SID)
        )

    def test_an_existing_worker_dir_is_kept(self):
        roster.worker_dir(SID)
        with open(os.path.join(self.path, "worker.json"), "w") as f:
            f.write("{}")
        self.assertEqual(roster.worker_dir(SID), self.path)
        self.assertEqual(os.listdir(self.path), ["worker.json"])
        roster.worker_dir(SID2)
        self.assertEqual(sorted(os.listdir(self.workers)), sorted([SID, SID2]))

    def test_a_bad_sid_refused_and_nothing_made(self):
        for sid in (SID.upper(), SID + "\n", SID[:-1], "", "..", f"../{SID}", f"{SID}/x", None, 7):
            with self.subTest(sid=sid), self.assertRaises(roster.RosterError) as cm:
                roster.worker_dir(sid)
            self.assertEqual(str(cm.exception), f"worker sid {sid!r}: want a session id")
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_workers_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.symlink(target, self.workers)
        self.refused(self.workers)
        self.assertEqual(os.listdir(target), [])

    def test_sid_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.mkdir(self.workers, 0o700)
        os.symlink(target, self.path)
        self.refused(self.path)
        self.assertEqual(os.listdir(target), [])

    def test_a_file_level_refused(self):
        open(self.workers, "w").close()
        self.refused(self.workers)
        os.unlink(self.workers)
        os.mkdir(self.workers, 0o700)
        open(self.path, "w").close()
        self.refused(self.path)

    def test_a_level_of_another_user_refused(self):
        os.mkdir(self.workers, 0o700)
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(self.workers)
        real = os.lstat

        def lstat(path, *args, **kw):
            st = real(path, *args, **kw)
            return (
                types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
                if path == self.path
                else st
            )

        with unittest.mock.patch("os.lstat", lstat):
            self.refused(self.path)

    def test_a_level_open_to_group_or_other_refused_and_not_chmodded(self):
        for level, bits in (
            (self.workers, 0o755),
            (self.workers, 0o701),
            (self.path, 0o750),
            (self.path, 0o707),
        ):
            with self.subTest(level=level, bits=oct(bits)):
                roster.worker_dir(SID)
                os.chmod(level, bits)
                self.refused(level, f"group or other permission bits set (mode {bits:04o})")
                self.assertEqual(mode(level), bits)
                os.chmod(level, 0o700)

    def test_agent_pm_a_file_refused(self):
        os.rmdir(self.agent_pm)
        open(self.agent_pm, "w").close()
        with self.assertRaisesRegex(roster.RosterError, f"^worker directory {self.agent_pm}: "):
            roster.worker_dir(SID)


class DefaultRuntimeTest(Base):
    def test_the_delegate_configs_runtime_with_the_local_file_on_top(self):
        base = read_project("delegate", local=os.devnull)["runtime"]
        self.assertFalse(os.path.exists(os.path.join(REPO, "manager", "config.toml")))
        self.assertEqual(roster.default_runtime(), base)
        self.local('runtime = "rt9"\n')
        self.assertEqual(roster.default_runtime(), "rt9")

    def test_a_runtime_not_a_non_empty_string_refused(self):
        for value in ('""', "5", "true", "[]", "{}", '["rt9"]'):
            with self.subTest(value=value):
                self.local(f"runtime = {value}\n")
                with self.assertRaises(roster.RosterError) as cm:
                    roster.default_runtime()
                self.assertEqual(str(cm.exception), NO_RUNTIME)

    def test_the_local_path_given(self):
        local = os.path.join(self.home, "mine.toml")
        with open(local, "w") as f:
            f.write('runtime = "rt7"\n')
        self.local('runtime = "rt9"\n')
        self.assertEqual(roster.default_runtime(local), "rt7")
        self.assertEqual(roster.default_runtime(), "rt9")

    def test_an_unknown_local_key_refused(self):
        self.local("bogus = 1\n")
        with self.assertRaisesRegex(roster.RosterError, "^config: .*unknown key 'bogus'"):
            roster.default_runtime()

    def test_an_unreadable_local_file_refused(self):
        self.local("runtime = \n")
        with self.assertRaisesRegex(roster.RosterError, "^config: .*line 1"):
            roster.default_runtime()
