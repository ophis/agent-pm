from new.tests.unit.adapters.codex.support import NATIVE, Base

import ast
import copy
import dataclasses
import json
import pathlib
import sys
from unittest import mock

from adapters.codex import tui_start

HOOK = (sys.executable, "-I", "/protected/driver_hook.py")


class StartTest(Base):
    def catalog(self):
        commands = {event: "command-" + event for event, name in tui_start.HOOKS}
        hooks = [
            {
                "source": "sessionFlags",
                "eventName": name,
                "enabled": True,
                "handlerType": "command",
                "command": commands[event],
                "key": "key-" + name,
                "currentHash": "sha256:" + "a" * 64,
            }
            for event, name in tui_start.HOOKS
        ]
        return {"data": [{"hooks": hooks, "errors": []}]}, commands

    def test_trust_exact_enabled_commands_and_hashes_only(self):
        result, commands = self.catalog()
        state = tui_start.trusted(result, str(self.cwd), commands)
        self.assertTrue(state.startswith("hooks.state={"))
        self.assertIn('"key-stop"={trusted_hash="sha256:', state)
        for field, value in (
            ("enabled", False),
            ("command", "foreign"),
            ("currentHash", "bad"),
            ("key", None),
        ):
            changed = copy.deepcopy(result)
            changed["data"][0]["hooks"][0][field] = value
            with self.subTest(field=field), self.assertRaises(tui_start.StartError):
                tui_start.trusted(changed, str(self.cwd), commands)
        for change in ({"data": []}, {"data": [{"hooks": []}]}, {"data": [{"hooks": None}]}):
            with self.assertRaises(tui_start.StartError):
                tui_start.trusted(change, str(self.cwd), commands)
        result["data"][0]["hooks"].append(
            {"source": "user", "enabled": True, "trustStatus": "modified", "key": "user"}
        )
        with self.assertRaisesRegex(tui_start.StartError, "review"):
            tui_start.trusted(result, str(self.cwd), commands)

    def test_answer_ignores_notifications_wrong_ids_and_malformed_json(self):
        lines = iter(
            [
                b"not json",
                b'{"id":true,"result":1}',
                b'{"method":"event","params":{}}',
                b'{"id":1,"result":{}}',
                b'{"id":2,"result":{"data":[]}}',
            ]
        )
        self.assertEqual(tui_start.answer(lines, 2, "hooks/list"), {"data": []})
        with self.assertRaisesRegex(tui_start.StartError, "denied"):
            tui_start.answer(iter([b'{"id":2,"error":{"message":"denied"}}']), 2, "hooks/list")

    def test_line_bounds_and_timeout(self):
        with mock.patch("time.monotonic", return_value=1):
            with self.assertRaisesRegex(tui_start.StartError, "no answer"):
                next(tui_start.Lines(42, 0))
        with (
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", return_value=b"x"),
        ):
            lines = tui_start.Lines(42, 999999999999)
            lines.buf = bytearray(tui_start.ANSWER_MAX)
            with self.assertRaisesRegex(tui_start.StartError, "over"):
                next(lines)

    def test_process_cleanup_kills_timed_out_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.side_effect = [tui_start.subprocess.TimeoutExpired("fake", 1), None]
        with (
            mock.patch("select.select", return_value=([], [], [])),
            mock.patch("os.killpg") as kill,
            mock.patch("time.monotonic", side_effect=[0, 0, 0, 6]),
        ):
            tui_start.close(proc)
        kill.assert_called_once_with(123, tui_start.signal.SIGKILL)
        proc.stdout.close.assert_called_once()

    def test_standalone_dependencies_use_python39_syntax_and_stdlib(self):
        directory = pathlib.Path(tui_start.__file__).parent
        for name in ("sessions.py", "tui_start.py", "tui_hook.py"):
            with self.subTest(name=name):
                tree = ast.parse((directory / name).read_text(), feature_version=(3, 9))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(
                            (node.module or "").startswith(("adapters", "driver", "utils", "core"))
                        )
                    if isinstance(node, ast.Import):
                        self.assertFalse(
                            any(
                                n.name.startswith(("adapters", "driver", "utils", "core"))
                                for n in node.names
                            )
                        )

    def test_start_adds_transient_trust_and_execs_saved_home_without_config_write(self):
        run = self.adapter.tui()
        hook = (sys.executable, "-I", "/protected/driver_hook.py")
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch("prompt", self.spec, False, hook)
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with (
            mock.patch.object(tui_start, "hook_trust", return_value="hooks.state={exact}") as trust,
            mock.patch("os.chdir") as chdir,
            mock.patch("os.execve") as execute,
        ):
            tui_start.main([launch.argv[-1]])
        chdir.assert_called_once_with(str(self.cwd))
        exe, argv, env = execute.call_args.args
        self.assertEqual(exe, "/protected/codex")
        self.assertEqual(argv[-4:], ["-c", "hooks.state={exact}", "--", "prompt"])
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(trust.call_args.args[2], str(self.cwd))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        )

    def test_hooks_list_initializes_and_closes_mock_process(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 42
        with (
            mock.patch.object(tui_start.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(tui_start, "answer", side_effect=[{}, {"data": []}]),
            mock.patch.object(tui_start, "close") as close,
        ):
            self.assertEqual(
                tui_start.hooks_list("/protected/fake", ["-c", "hook"], str(self.cwd), {}),
                {"data": []},
            )
        self.assertEqual(popen.call_args.args[0], ["/protected/fake", "app-server", "-c", "hook"])
        sent = [json.loads(call.args[0]) for call in proc.stdin.write.call_args_list]
        self.assertEqual(
            [msg["method"] for msg in sent], ["initialize", "initialized", "hooks/list"]
        )
        self.assertEqual(sent[-1]["params"], {"cwds": [str(self.cwd)]})
        close.assert_called_once_with(proc)

    def test_retry_once_after_appserver_exit_and_start_lock_timeout(self):
        self.prepare()
        result, commands = self.catalog()
        with (
            mock.patch.object(
                tui_start, "hooks_list", side_effect=[tui_start._Exited(1), result]
            ) as listing,
            mock.patch("time.sleep"),
        ):
            self.assertTrue(
                tui_start.hook_trust(
                    "fake", [], str(self.cwd), {"CODEX_HOME": str(self.codex_home)}, commands
                ).startswith("hooks.state=")
            )
        self.assertEqual(listing.call_count, 2)
        with (
            mock.patch("fcntl.flock", side_effect=BlockingIOError),
            mock.patch("time.monotonic", side_effect=[0, 31]),
        ):
            with self.assertRaisesRegex(tui_start.StartError, "held"):
                tui_start.hook_trust(
                    "fake", [], str(self.cwd), {"CODEX_HOME": str(self.codex_home)}, commands
                )

    def test_missing_duplicate_foreign_hook_rows_and_disabled_user_hooks(self):
        result, commands = self.catalog()
        rows = result["data"][0]["hooks"]
        for changed in (
            rows[1:],
            [*rows, rows[0]],
            [*rows, {"source": "sessionFlags", "eventName": "foreign"}],
        ):
            with self.assertRaises(tui_start.StartError):
                tui_start.trusted({"data": [{"hooks": changed}]}, str(self.cwd), commands)
        for extra in (
            {"source": "user", "enabled": False, "trustStatus": "modified"},
            {"source": "user", "enabled": True, "isManaged": True, "trustStatus": "untrusted"},
            {"source": "user", "enabled": True, "trustStatus": "trusted"},
        ):
            self.assertTrue(
                tui_start.trusted(
                    {"data": [{"hooks": [*rows, extra]}]}, str(self.cwd), commands
                ).startswith("hooks.state=")
            )

    def test_bad_launch_data_does_not_spawn(self):
        with mock.patch.object(tui_start.subprocess, "Popen") as proc:
            for data in ("null", "not-json", "{}"):
                with self.assertRaises(tui_start.StartError):
                    tui_start.main([data])
        proc.assert_not_called()

    def test_signal_during_spawn_cleans_owned_process_without_retry_or_exec(self):
        run = self.adapter.tui()
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(
                "prompt", self.spec, False, (sys.executable, "-I", "/protected/hook.py")
            )
        for signum in (tui_start.signal.SIGHUP, tui_start.signal.SIGTERM):
            with self.subTest(signum=signum):
                proc = mock.Mock(pid=123)
                proc.stdout.fileno.return_value = 42
                proc.wait.return_value = 0
                original = tui_start.signal.getsignal(signum)

                def spawn(*args, **kwargs):
                    tui_start.signal.raise_signal(signum)
                    return proc

                with (
                    mock.patch("subprocess.Popen", side_effect=spawn) as popen,
                    mock.patch("os.killpg") as kill,
                    mock.patch("os.execve") as execute,
                    self.assertRaises(tui_start._Terminated),
                ):
                    tui_start.main([launch.argv[-1]])
                self.assertEqual(popen.call_count, 1)
                kill.assert_called_with(123, tui_start.signal.SIGKILL)
                proc.wait.assert_called()
                proc.stdout.close.assert_called_once()
                execute.assert_not_called()
                self.assertEqual(tui_start.signal.getsignal(signum), original)

    def test_normal_probe_exit_kills_remaining_owned_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.return_value = 0
        with (
            mock.patch("select.select", return_value=([], [], [])),
            mock.patch("os.killpg") as kill,
        ):
            tui_start.close(proc)
        kill.assert_called_once_with(123, tui_start.signal.SIGKILL)
        proc.wait.assert_called()

    def test_wrapper_checks_effective_tmpdir_before_spawning(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = self.adapter.tui().launch(
                "hi", self.spec, False, (sys.executable, "-I", "/protected/hook.py")
            )
        with (
            mock.patch.dict("os.environ", {"TMPDIR": "/protected"}),
            mock.patch("subprocess.Popen") as proc,
            mock.patch("os.execve") as execute,
            self.assertRaisesRegex(tui_start.sessions.RecoveryError, "rewrite|writable"),
        ):
            tui_start.main([launch.argv[-1]])
        proc.assert_not_called()
        execute.assert_not_called()


class ProtectTest(Base):
    def launch(self, spec, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            return self.adapter.tui().launch("prompt", spec, resume, HOOK).argv[-1]

    def test_the_recheck_keeps_carried_protect_paths_out_of_the_saved_roots(self):
        link = self.root / "protected-link"
        flags = ("--add-dir", "../../guarded", "--add-dir=../../other")
        cases = (
            (False, flags, self.root / "guarded" / "events"),
            (False, flags, self.root / "other" / "events"),
            (False, flags, self.root),
            (False, (), self.cwd),
            (True, (), self.extra / "events"),
        )
        for index, (role_policy, flags, target) in enumerate(cases):
            link.unlink(missing_ok=True)
            link.symlink_to("/protected/events")
            spec = dataclasses.replace(
                self.spec,
                sid=f"recheck-{index}",
                role_policy=role_policy,
                flags=flags,
                protect=(str(link),),
            )
            data = self.launch(spec)
            self.assertEqual(json.loads(data)["protect"], [str(link)])
            link.unlink()
            link.symlink_to(target)
            with (
                self.subTest(role_policy=role_policy, target=target),
                mock.patch("subprocess.Popen") as proc,
                mock.patch("os.execve") as execute,
                self.assertRaisesRegex(
                    tui_start.sessions.RecoveryError, f"could rewrite hook dependency {target}$"
                ),
            ):
                tui_start.main([data])
            proc.assert_not_called()
            execute.assert_not_called()

    def test_bad_protect_launch_data_does_not_spawn(self):
        data = json.loads(self.launch(self.spec))
        with mock.patch.object(tui_start.subprocess, "Popen") as proc:
            for key in ("protect", "add_dirs"):
                for paths in ("/protected", ["relative"], [1], ["/a\0b"], None):
                    with (
                        self.subTest(key=key, paths=paths),
                        self.assertRaises(tui_start.StartError),
                    ):
                        tui_start.main([json.dumps({**data, key: paths})])
                with self.assertRaisesRegex(tui_start.StartError, "fields"):
                    tui_start.main([json.dumps({k: v for k, v in data.items() if k != key})])
        proc.assert_not_called()

    def test_the_recheck_covers_a_resumes_added_dirs(self):
        self.launch(dataclasses.replace(self.spec, role_policy=False))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        link = self.root / "protected-link"
        cases = (
            (("--add-dir", "../../guarded"), self.root / "guarded" / "events"),
            (("--add-dir=../../other/sub",), self.root / "other"),
            ((f"--add-dir={self.root / 'third'}",), self.root / "third" / "events"),
        )
        for flags, target in cases:
            link.unlink(missing_ok=True)
            link.symlink_to("/protected/events")
            spec = dataclasses.replace(
                self.spec, role_policy=False, flags=flags, protect=(str(link),)
            )
            data = self.launch(spec, resume=True)
            link.unlink()
            link.symlink_to(target)
            with (
                self.subTest(flags=flags),
                mock.patch("subprocess.Popen") as proc,
                mock.patch("os.execve") as execute,
                self.assertRaisesRegex(
                    tui_start.sessions.RecoveryError, f"could rewrite hook dependency {target}$"
                ),
            ):
                tui_start.main([data])
            proc.assert_not_called()
            execute.assert_not_called()

    def test_a_plain_start_lists_hooks_with_the_users_config_and_execs_the_flags(self):
        flags = ("-c", 'model_verbosity="low"', "--disable", "web", "-m", "o3")
        spec = dataclasses.replace(self.spec, role_policy=False, flags=flags)
        data = self.launch(spec)
        with (
            mock.patch.object(tui_start, "hook_trust", return_value="hooks.state={exact}") as trust,
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.main([data])
        argv = execute.call_args.args[1]
        self.assertEqual(argv[-10:], [*flags, "-c", "hooks.state={exact}", "--", "prompt"])
        hook_flags = argv[10:22]
        self.assertEqual(
            trust.call_args.args[1],
            [*hook_flags, "-c", 'model_verbosity="low"', "--disable", "web"],
        )
