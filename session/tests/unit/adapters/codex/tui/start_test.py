"""Codex startup package bootstrap, trust, launch validation and process cleanup."""

import contextlib
import copy
import dataclasses
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
from unittest import mock

from agent_pm.session.adapters.codex import tui
from agent_pm.session.adapters.codex.tui import start as tui_start

from session.tests import case
from session.tests.case import Case
from session.tests.source_roots import PACKAGE_ROOTS, source_roots_cases
from session.tests.unit.adapters.codex.support import NATIVE, Base

DIRECTORY = pathlib.Path(tui_start.__file__).parent
ROOTS_JSON = json.dumps([case.SRC["session"], case.SRC["utils"]])
HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "/protected/session", ROOTS_JSON)


class BootstrapTest(Case):
    def test_main_reports_launch_validation_as_one_line_and_failure_status(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(tui_start.main({}), 1)
        self.assertEqual(stderr.getvalue(), "codex tui: Codex tui launch: invalid launch fields\n")


class StartTest(Base):
    def catalog(self):
        commands = {event: "command-" + event for event, name, _ in tui_start.HOOKS}
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
            for event, name, _ in tui_start.HOOKS
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

    def test_opaque_hook_words_survive_launch_and_exact_trust_matching(self):
        for index, hook in enumerate(
            (
                (*HOOK, "extra opaque word"),
                (*HOOK[:-1], json.dumps(PACKAGE_ROOTS, indent=2)),
                (*HOOK[:-1], "not JSON at all"),
            )
        ):
            spec = dataclasses.replace(self.spec, sid=f"opaque-trust-{index}")
            with (
                self.subTest(hook=hook),
                mock.patch("shutil.which", return_value="/protected/codex"),
            ):
                launch = self.adapter.tui().launch(
                    dataclasses.replace(spec, prompt="prompt"), hook, source_roots=PACKAGE_ROOTS
                )
                plan = tui.decode(json.loads(launch.argv[-1]))
                catalog, _ = self.catalog()
                rows = catalog["data"][0]["hooks"]
                for row, (event, _, _) in zip(rows, tui_start.HOOKS, strict=True):
                    row["command"] = shlex.join((*hook, event))
                commands = tui.commands(plan.hook)
                trusted = tui_start.trusted(catalog, str(self.cwd), commands)
                self.assertIn('"key-stop"={trusted_hash="sha256:', trusted)
                rows[0]["command"] = shlex.join((*hook[:-1], "SessionStart"))
                with self.assertRaises(tui_start.StartError):
                    tui_start.trusted(catalog, str(self.cwd), commands)

    def test_start_adds_transient_trust_and_execs_saved_home_without_config_write(self):
        run = self.adapter.tui()
        hook = HOOK
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(
                dataclasses.replace(self.spec, prompt="prompt"), hook, source_roots=PACKAGE_ROOTS
            )
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with (
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir") as chdir,
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(launch.argv[-1]), trust=trust)
        chdir.assert_called_once_with(str(self.cwd))
        exe, argv, env = execute.call_args.args
        self.assertEqual(exe, "/protected/codex")
        self.assertEqual(argv[-4:], ["-c", "hooks.state={exact}", "--", "prompt"])
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(trust.call_args.kwargs["cwd"], str(self.cwd))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        )

    def test_start_passes_the_session_env_to_codex(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        spec = dataclasses.replace(self.spec, access=access)
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = self.adapter.tui().launch(
                dataclasses.replace(spec, prompt="prompt"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
                source_roots=PACKAGE_ROOTS,
            )
        with (
            mock.patch.dict(os.environ, launch.env),
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(launch.argv[-1]), trust=trust)
        self.assertEqual(execute.call_args.args[2]["PYTHONPATH"], "/a b;c")

    def test_hooks_list_initializes_and_closes_mock_process(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 42
        with (
            mock.patch.object(subprocess, "Popen", return_value=proc) as popen,
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch(
                "os.read",
                side_effect=[
                    b'{"id":1,"result":{}}\n{"id":2,"result":{"data":[]}}\n',
                    b" ".strip(),
                ],
            ),
            mock.patch("os.killpg"),
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
        proc.stdin.close.assert_called_once_with()
        proc.stdout.close.assert_called_once_with()

    def test_retry_once_after_appserver_exit_and_start_lock_timeout(self):
        self.prepare()
        result, commands = self.catalog()
        with (
            contextlib.nullcontext(
                mock.Mock(side_effect=[tui_start.app_server.Exited(1), result])
            ) as listing,
            mock.patch("time.sleep"),
        ):
            self.assertTrue(
                tui_start.hook_trust(
                    exe="fake",
                    args=[],
                    cwd=str(self.cwd),
                    env={"CODEX_HOME": str(self.codex_home)},
                    listing=listing,
                    commands=commands,
                ).startswith("hooks.state=")
            )
        self.assertEqual(listing.call_count, 2)
        with (
            mock.patch("fcntl.flock", side_effect=BlockingIOError),
            mock.patch("time.monotonic", side_effect=[0, 31]),
            self.assertRaisesRegex(tui_start.StartError, "held"),
        ):
            tui_start.hook_trust(
                exe="fake",
                args=[],
                cwd=str(self.cwd),
                env={"CODEX_HOME": str(self.codex_home)},
                commands=commands,
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
        with mock.patch.object(subprocess, "Popen") as proc:
            for data in ("null", "not-json", "{}"):
                with self.assertRaises(tui_start.StartError):
                    tui_start.launch(tui_start._bootstrap([data]))
        proc.assert_not_called()

    def test_import_roots_are_required_and_validated_without_spawning(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = self.adapter.tui().launch(
                dataclasses.replace(self.spec, prompt="prompt"), HOOK, source_roots=PACKAGE_ROOTS
            )
        data = json.loads(launch.argv[-1])
        missing = {key: value for key, value in data.items() if key != "import_roots"}
        _, invalid = source_roots_cases(self.root)
        with mock.patch("subprocess.Popen") as proc, mock.patch("os.execve") as execute:
            for changed in (
                missing,
                *(data | {"import_roots": roots} for roots in invalid.values()),
            ):
                with (
                    self.subTest(roots=changed.get("import_roots")),
                    self.assertRaises(tui_start.StartError),
                ):
                    tui_start.launch(tui_start._bootstrap([json.dumps(changed)]))
            with self.assertRaisesRegex(ValueError, "fields"):
                tui_start.launch(data | {"unknown": []})
        proc.assert_not_called()
        execute.assert_not_called()

    def test_signal_during_spawn_cleans_owned_process_without_retry_or_exec(self):
        run = self.adapter.tui()
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(
                dataclasses.replace(self.spec, prompt="prompt"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
                source_roots=PACKAGE_ROOTS,
            )
        for signum in (tui_start.signal.SIGHUP, tui_start.signal.SIGTERM):
            with self.subTest(signum=signum):
                proc = mock.Mock(pid=123)
                proc.stdout.fileno.return_value = 42
                proc.wait.return_value = 0
                original = tui_start.signal.getsignal(signum)

                def spawn(*_args, proc=proc, signum=signum, **_kwargs):
                    tui_start.signal.raise_signal(signum)
                    return proc

                with (
                    mock.patch("subprocess.Popen", side_effect=spawn) as popen,
                    mock.patch("os.killpg") as kill,
                    mock.patch("os.execve") as execute,
                    self.assertRaises(tui_start.Stopped),
                ):
                    tui_start.launch(json.loads(launch.argv[-1]))
                self.assertEqual(popen.call_count, 1)
                kill.assert_called_with(123, tui_start.signal.SIGKILL)
                proc.wait.assert_called()
                proc.stdout.close.assert_called_once()
                execute.assert_not_called()
                self.assertEqual(tui_start.signal.getsignal(signum), original)

    def test_wrapper_checks_effective_tmpdir_before_spawning(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = self.adapter.tui().launch(
                dataclasses.replace(self.spec, prompt="hi"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
                source_roots=PACKAGE_ROOTS,
            )
        with (
            mock.patch.dict("os.environ", {"TMPDIR": "/protected"}),
            mock.patch("subprocess.Popen") as proc,
            mock.patch("os.execve") as execute,
            self.assertRaisesRegex(tui_start.common.RecoveryError, "rewrite|writable"),
        ):
            tui_start.launch(json.loads(launch.argv[-1]))
        proc.assert_not_called()
        execute.assert_not_called()


class ProtectTest(Base):
    def launch(self, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            return (
                self.adapter.tui()
                .launch(
                    dataclasses.replace(spec, prompt="prompt", resume=resume),
                    HOOK,
                    source_roots=PACKAGE_ROOTS,
                )
                .argv[-1]
            )

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
                flags=flags,
                protect=(str(link),),
            )
            data = self.launch(spec)
            self.assertEqual(json.loads(data)["protect"], [*case.ROOTS, str(link)])
            link.unlink()
            link.symlink_to(target)
            with (
                self.subTest(role_policy=role_policy, target=target),
                mock.patch("subprocess.Popen") as proc,
                mock.patch("os.execve") as execute,
                self.assertRaisesRegex(
                    tui_start.common.RecoveryError, f"could rewrite hook dependency {target}$"
                ),
            ):
                tui_start.launch(json.loads(data))
            proc.assert_not_called()
            execute.assert_not_called()

    def test_bad_protect_launch_data_does_not_spawn(self):
        data = json.loads(self.launch(self.spec))
        with mock.patch.object(subprocess, "Popen") as proc:
            for key in ("protect",):
                for paths in ("/protected", ["relative"], [1], ["/a\0b"], None):
                    with (
                        self.subTest(key=key, paths=paths),
                        self.assertRaises(ValueError),
                    ):
                        tui_start.launch({**data, key: paths})
                with self.assertRaisesRegex(ValueError, "fields"):
                    tui_start.launch({k: v for k, v in data.items() if k != key})
        proc.assert_not_called()

    def test_the_recheck_covers_saved_flags_on_resume(self):
        link = self.root / "protected-link"
        target = self.root / "guarded" / "events"
        flags = ("--add-dir", "../../guarded")
        self.launch(dataclasses.replace(self.spec, flags=flags))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        link.symlink_to("/protected/events")
        data = self.launch(dataclasses.replace(self.spec, protect=(str(link),)), resume=True)
        link.unlink()
        link.symlink_to(target)
        with (
            mock.patch("subprocess.Popen") as proc,
            mock.patch("os.execve") as execute,
            self.assertRaisesRegex(tui_start.common.RecoveryError, "could rewrite hook dependency"),
        ):
            tui_start.launch(json.loads(data))
        proc.assert_not_called()
        execute.assert_not_called()

    def test_a_plain_start_lists_hooks_with_the_users_config_and_execs_the_flags(self):
        flags = ("-c", 'model_verbosity="low"', "--disable", "web", "-m", "o3")
        spec = dataclasses.replace(self.spec, flags=flags)
        data = self.launch(spec)
        with (
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(data), trust=trust)
        argv = execute.call_args.args[1]
        start = argv.index("-c", 6)
        self.assertEqual(argv[start : start + len(flags)], list(flags))
        hook_flags = tui.hook_flags(tui.decode(json.loads(data)).hook)
        self.assertEqual(
            trust.call_args.kwargs["args"],
            [*hook_flags, "-c", 'model_verbosity="low"', "--disable", "web"],
        )
