import os
import stat
import subprocess
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import manager  # noqa: E402

INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3"}


def tmux(out="own\n"):
    """A proc answering every call with out; its calls are recorded."""
    def proc(argv, **kw):
        proc.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    proc.calls = []
    return proc


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.managers = os.path.join(self.agent_pm, "managers")
        self.dir = os.path.join(self.managers, "m1")
        self.enterContext(unittest.mock.patch.dict(os.environ))
        for key in INSIDE:
            os.environ.pop(key, None)

    def umask(self, mask):
        self.addCleanup(os.umask, os.umask(mask))

    def outside(self, name):
        """A directory beside ~/.agent-pm, for a link to point at."""
        path = os.path.join(os.path.dirname(self.agent_pm), name)
        os.mkdir(path)
        return path

    def test_manager_beats_tmux(self):
        proc = tmux()
        os.environ.update(INSIDE)
        self.assertEqual(manager.directory("m1", proc=proc), self.dir)
        self.assertEqual(proc.calls, [])

    def test_own_session(self):
        proc = tmux("own\n")
        os.environ.update(INSIDE)
        self.assertEqual(manager.directory(proc=proc), os.path.join(self.managers, "own"))
        self.assertEqual(len(proc.calls), 1)

    def test_outside_tmux_is_none(self):
        proc = tmux()
        self.assertIsNone(manager.directory(proc=proc))
        self.assertEqual(proc.calls, [])

    def test_resolving_touches_nothing(self):
        os.environ.update(INSIDE)
        manager.directory("m1")
        manager.directory(proc=tmux())
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_relative_home_gives_an_absolute_path(self):
        os.environ["HOME"] = "rel/home"
        want = os.path.join(os.path.abspath("rel/home"), ".agent-pm", "managers", "m1")
        self.assertEqual(manager.directory("m1"), want)

    def test_bad_manager_names(self):
        for name in ("a/b", "..", "", "a b", "a;", "a\n"):
            with self.subTest(name=name), self.assertRaises(manager.ManagerError) as cm:
                manager.directory(name)
            self.assertEqual(str(cm.exception), f"manager {name!r}: want [A-Za-z0-9_-]+")

    def test_bad_own_session_name_names_manager_option(self):
        os.environ.update(INSIDE)
        with self.assertRaises(manager.ManagerError) as cm:
            manager.directory(proc=tmux("a;\n"))
        self.assertEqual(str(cm.exception), "own tmux session 'a;': want [A-Za-z0-9_-]+; give --manager <name>")

    def test_bad_tmux_pane_names_manager_option(self):
        os.environ.update(INSIDE, TMUX_PANE="%3;")
        proc = tmux()
        with self.assertRaisesRegex(manager.ManagerError, r"TMUX_PANE.*; give --manager <name>$"):
            manager.directory(proc=proc)
        self.assertEqual(proc.calls, [])

    def test_events_path_without_create_touches_nothing(self):
        self.assertEqual(manager.events(self.dir, create=False), os.path.join(self.dir, "events"))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def check_created(self, mask):
        os.rmdir(self.agent_pm)
        self.umask(mask)
        path = manager.events(self.dir)
        self.assertEqual(path, os.path.join(self.dir, "events"))
        for made in (self.agent_pm, self.managers, self.dir):
            self.assertEqual(mode(made), 0o700, made)
        self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
        self.assertEqual(mode(path), 0o600)

    def test_created_under_umask_022(self):
        self.check_created(0o022)

    def test_created_under_umask_077(self):
        self.check_created(0o077)

    def test_directories_are_0700_even_when_the_umask_strips_owner_bits(self):
        self.umask(0o277)
        manager.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o700, 0o700])

    def test_existing_managers_directory_keeps_its_mode(self):
        os.mkdir(self.managers)
        os.chmod(self.managers, 0o755)
        manager.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o755, 0o700])

    def test_existing_directory_is_kept(self):
        manager.ensure(self.dir)
        os.chmod(self.dir, 0o750)
        with open(manager.events(self.dir), "w") as f:
            f.write("12:00:00 w1 done\n")
        manager.ensure(self.dir)
        self.assertEqual(mode(self.dir), 0o750)
        with open(os.path.join(self.dir, "events")) as f:
            self.assertEqual(f.read(), "12:00:00 w1 done\n")

    def refused(self, path):
        with self.assertRaises(manager.ManagerError) as cm:
            manager.events(self.dir)
        self.assertEqual(str(cm.exception), f"manager directory {path}: not a directory owned by you")

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
        with self.assertRaisesRegex(manager.ManagerError, f"^manager directory {self.agent_pm}: "):
            manager.ensure(self.dir)

    def test_events_symlink_refused(self):
        manager.ensure(self.dir)
        target = os.path.join(self.outside("target"), "file")
        open(target, "w").close()
        os.symlink(target, os.path.join(self.dir, "events"))
        with self.assertRaisesRegex(manager.ManagerError, "events file .*events"):
            manager.events(self.dir)
        self.assertEqual(os.path.getsize(target), 0)


if __name__ == "__main__":
    unittest.main()
