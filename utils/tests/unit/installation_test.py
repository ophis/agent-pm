"""Retained runtime trees, path relocation and lease cleanup."""

import fcntl
import json
import signal
from pathlib import Path
from unittest import mock

from agent_pm.utils import installation

from utils.tests.case import Case


class InstallationTest(Case):
    def setUp(self):
        self.source = Path(self.home) / "cache A"
        self.root = Path(self.home) / "kept"
        (self.source / "bin").mkdir(parents=True)
        (self.source / "utils/src/agent_pm/utils").mkdir(parents=True)
        self.command = self.source / "bin/agent-pm-session"
        self.command.write_text("#!/bin/sh\nexit 0\n")
        self.command.chmod(0o755)
        self.resource = self.source / "utils/src/agent_pm/utils/data.json"
        self.resource.write_text('{"resource": true}')

    def keep(self):
        return installation.Installation(str(self.source), root=str(self.root))

    def test_copies_resources_modes_and_internal_symlinks_without_development_files(self):
        (self.source / "utils/tests").mkdir()
        (self.source / "utils/tests/ignored.py").write_text("unused")
        (self.resource.parent / "alias.json").symlink_to(self.resource)
        with self.keep() as kept:
            copy = Path(kept.root)
            self.assertEqual(
                (copy / "utils/src/agent_pm/utils/alias.json").read_text(),
                self.resource.read_text(),
            )
            self.assertFalse((copy / "utils/src/agent_pm/utils/alias.json").is_symlink())
            self.assertEqual((copy / "bin/agent-pm-session").stat().st_mode & 0o777, 0o755)
            self.assertFalse((copy / "utils/tests").exists())
        self.assertFalse(copy.exists())

    def test_maps_nested_encoded_arguments_and_prepends_preserved_bin(self):
        with self.keep() as kept:
            original = {"hook": [str(self.command)], "roots": [str(self.resource.parent)]}
            mapped = kept.relocate(original)
            self.assertEqual(mapped["hook"], [str(Path(kept.root) / "bin/agent-pm-session")])
            encoded = kept.relocate(json.dumps(original))
            self.assertEqual(json.loads(encoded), mapped)
            env = kept.environment({"PATH": "/usr/bin", "PYTHONPATH": str(self.resource.parent)})
            self.assertEqual(env["PATH"], kept.root + "/bin:/usr/bin")
            self.assertEqual(env["PYTHONPATH"], mapped["roots"][0])
            self.assertEqual(env[installation.ORIGIN_ENV], str(self.source))
            command = kept.command(("python3", "-m", "module", json.dumps(original)))
            self.assertEqual(json.loads(command[-1])[-1], encoded)
            kept.discard()

    def test_failed_copy_cleans_partial_tree_and_refuses_external_symlink(self):
        (self.resource.parent / "outside").symlink_to(Path(self.home) / "secret")
        with self.assertRaisesRegex(ValueError, "outside installation"):
            self.keep()
        self.assertEqual(list(self.root.iterdir()), [])
        (self.resource.parent / "outside").unlink()
        with (
            mock.patch("shutil.copytree", side_effect=OSError("copy refused")),
            self.assertRaisesRegex(OSError, "copy refused"),
        ):
            self.keep()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sweep_keeps_active_and_handoff_copies_then_removes_abandoned_ones(self):
        with self.keep() as kept:
            folder = Path(kept.root)
            kept.command(("true",))
            installation.sweep(str(self.root), now=folder.stat().st_mtime + 120)
            self.assertTrue(folder.exists())
        installation.sweep(str(self.root), now=folder.stat().st_mtime)
        self.assertTrue(folder.exists())
        installation.sweep(str(self.root), now=folder.stat().st_mtime + 120)
        self.assertFalse(folder.exists())

    def test_failure_after_handoff_discards_and_sweep_never_follows_foreign_paths(self):
        with self.assertRaisesRegex(RuntimeError, "spawn"), self.keep() as kept:
            folder = Path(kept.root)
            kept.command(("true",))
            raise RuntimeError("spawn")
        self.assertFalse(folder.exists())
        foreign = Path(self.home) / "foreign"
        foreign.mkdir()
        (self.root / "run-alias").symlink_to(foreign, target_is_directory=True)
        installation.sweep(str(self.root), now=10**12)
        self.assertTrue(foreign.exists())
        self.assertTrue((self.root / "run-alias").is_symlink())

    def test_private_root_and_lease_symlinks_are_refused(self):
        self.root.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "private directory"):
            self.keep()
        self.root.unlink()
        with self.keep() as kept:
            lease = Path(kept.root) / ".lease"
            lease.unlink()
            lease.symlink_to(self.command)
            with self.assertRaises(OSError):
                installation.retire(kept.root)

    def test_retire_uses_an_independent_lock_descriptor(self):
        with self.keep() as kept:
            self.assertFalse(installation.retire(kept.root))
            with (
                open(Path(kept.root) / ".lease", "rb") as stream,
                self.assertRaises(BlockingIOError),
            ):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_relocation_does_not_rewrite_similarly_named_installations(self):
        source = str(self.source)
        self.assertEqual(
            installation.relocate(source + "-other/bin", source, "/kept"), source + "-other/bin"
        )
        self.assertEqual(
            installation.relocate({"n": 1, "v": None}, source, "/kept"), {"n": 1, "v": None}
        )
        self.assertEqual(installation.relocate((source + "/bin",), source, "/kept"), ("/kept/bin",))

    def test_supervisor_returns_child_code_and_cleans_after_wait(self):
        for code, want in ((0, 0), (7, 7), (-15, 143)):
            with self.subTest(code=code):
                child = mock.Mock()
                child.wait.return_value = code
                child.poll.return_value = code
                with self.keep() as kept:
                    folder = kept.root
                    kept.command(("child",))
                with (
                    mock.patch("subprocess.Popen", return_value=child) as spawn,
                    mock.patch("signal.signal"),
                    mock.patch("signal.pthread_sigmask", return_value=set()),
                ):
                    self.assertEqual(installation.supervise(folder, ("child",)), want)
                self.assertFalse(Path(folder).exists())
                self.assertEqual(spawn.call_args.args, (("child",),))
                self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)

    def test_supervisor_forwards_early_signal_and_does_not_block_child_signals(self):
        handlers = {}
        child = mock.Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0

        def handler(signum, value):
            handlers[signum] = value
            return signal.SIG_DFL

        def spawn(*_args, **_kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return child

        with self.keep() as kept:
            folder = kept.root
            kept.command(("child",))
        with (
            mock.patch("subprocess.Popen", side_effect=spawn),
            mock.patch("signal.signal", side_effect=handler),
            mock.patch("signal.pthread_sigmask", return_value=set()) as mask,
        ):
            installation.supervise(folder, ("child",))
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(mask.call_args_list[0], mock.call(signal.SIG_BLOCK, ()))

    def test_supervisor_failed_spawn_releases_copy(self):
        with self.keep() as kept:
            folder = kept.root
            kept.command(("child",))
        with (
            mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")),
            mock.patch("signal.signal"),
            mock.patch("signal.pthread_sigmask", return_value=set()),
            self.assertRaisesRegex(OSError, "spawn failed"),
        ):
            installation.supervise(folder, ("child",))
        self.assertFalse(Path(folder).exists())
