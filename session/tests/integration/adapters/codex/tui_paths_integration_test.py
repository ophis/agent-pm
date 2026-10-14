"""Codex source-root and symlink policies against real filesystem paths."""

import dataclasses
import json
import shlex
import tomllib
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.codex import tui
from agent_pm.session.adapters.codex.tui import HOOKS, HookPlan
from agent_pm.session.adapters.codex.tui import start as tui_start
from agent_pm.session.engine.types import ConfigError

from session.tests import case
from session.tests.case import SRC
from session.tests.codex_tui import HOOK, PlanCase, ProtectCase
from session.tests.source_roots import PACKAGE_ROOTS, source_roots_cases
from session.tests.unit.adapters.codex.support import NATIVE, Base


class StartPaths(Base):
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


class ProtectPaths(ProtectCase):
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


class TuiPaths(Base):
    def test_writable_symlink_to_protected_driver_hook_is_rejected(self):
        link = self.cwd / "driver-link.py"
        link.symlink_to("/protected/driver_hook.py")
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaisesRegex(ConfigError, "rewrite|writable"),
        ):
            self.adapter.tui().launch(
                dataclasses.replace(self.spec, prompt="hi"),
                (*HOOK[:2], str(link), *HOOK[3:]),
                source_roots=PACKAGE_ROOTS,
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid))


class PlanPaths(PlanCase):
    def test_hook_commands_preserve_roots_quoting_and_all_six_timeouts(self):
        roots = []
        for package, name in (("session", "session 'sources"), ("utils", "utils sources 字")):
            root = Path(self.home) / name
            root.symlink_to(SRC[package], target_is_directory=True)
            roots.append(str(root))
        argv = (
            *self.plan.hook.argv,
            json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
            "opaque trailing word",
        )
        hook = HookPlan(argv, tuple(roots))
        self.assertEqual(hook.argv, argv)
        self.assertEqual(hook.import_roots, tuple(roots))
        decoded = tui.decode(json.loads(tui.encode(dataclasses.replace(self.plan, hook=hook))))
        self.assertEqual(decoded.hook, hook)
        commands = tui.commands(hook)
        self.assertEqual(list(commands), [event for event, _, _ in HOOKS])
        flags = tui.hook_flags(hook)
        self.assertEqual(flags[::2], ("-c",) * 6)
        for (event, _, _), encoded in zip(HOOKS, flags[1::2], strict=True):
            with self.subTest(event=event):
                self.assertEqual(shlex.split(commands[event]), [*argv, event])
                row = tomllib.loads(encoded)["hooks"][event][0]["hooks"][0]
                self.assertEqual(
                    row,
                    {
                        "type": "command",
                        "command": commands[event],
                        "timeout": 3 if event == "Interrupt" else 10,
                    },
                )
