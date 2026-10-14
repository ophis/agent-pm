"""Codex launch plans preserve one flag authority and shared hook commands across the wire."""

import dataclasses
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.codex import tui_plan
from agent_pm.session.adapters.codex.hook_schema import HOOKS
from agent_pm.session.adapters.codex.types import HookPlan, LaunchPlan, Snapshot

from session.tests.case import SRC, Case


class PlanTest(Case):
    def setUp(self):
        self.snapshot = Snapshot(
            cwd="/work",
            project=True,
            dirs=("/granted",),
            commands=(),
            model="configured",
            effort=None,
            codex_home="/runtime/codex",
            flags=(
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "--",
                "--config=literal",
                "--disable",
                "literal",
            ),
            env={},
        )
        self.plan = LaunchPlan(
            executable="/protected/codex",
            selection=("resume", "saved-native"),
            prompt="prompt\nwith Unicode: 字",
            hook=HookPlan(
                (os.path.realpath(sys.executable), "-I", "/protected/mode hook.py", "/session dir"),
                (SRC["session"], SRC["utils"]),
            ),
            workdir="/work",
            sid="caller-id",
            store="/private-state/recovery/codex",
            protect=("/protected/events",),
        )
        self.frontend = (
            "/protected/codex",
            "resume",
            "saved-native",
            "--no-daemon",
            "-C",
            "/work",
            "-c",
            'tui.notification_method="bel"',
        )

    def test_wire_roundtrip_decodes_an_object_without_reparsing_json(self):
        encoded = tui_plan.encode(self.plan)
        data = json.loads(encoded)
        self.assertEqual(
            set(data),
            {
                "executable",
                "selection",
                "prompt",
                "hook",
                "workdir",
                "sid",
                "store",
                "protect",
                "import_roots",
            },
        )
        self.assertEqual(data["hook"], list(self.plan.hook.argv))
        self.assertEqual(data["import_roots"], list(self.plan.hook.import_roots))
        with mock.patch("json.loads", side_effect=AssertionError("launch JSON decoded twice")):
            decoded = tui_plan.decode(data)
            with self.assertRaisesRegex(ValueError, "launch fields"):
                tui_plan.decode(encoded)
        self.assertEqual(decoded, self.plan)

    def test_decoded_plan_is_detached_from_mutable_wire_input(self):
        encoded = tui_plan.encode(self.plan)
        data = json.loads(encoded)
        decoded = tui_plan.decode(data)
        for key in ("selection", "hook", "protect", "import_roots"):
            data[key].append("changed")
        data["prompt"] = "changed"
        self.assertEqual(decoded, self.plan)
        self.assertEqual(tui_plan.encode(decoded), encoded)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decoded.prompt = "changed"

    def test_wire_rejects_competing_command_and_directory_authorities(self):
        data = json.loads(tui_plan.encode(self.plan))
        for key, value in (
            ("argv", ["/foreign/codex"]),
            ("prefix", ["/foreign/codex", "--add-dir", "/foreign-write-root"]),
            ("commands", {"Stop": "foreign"}),
            ("hook_flags", ["-c", "hooks.Stop=[]"]),
            ("add_dirs", ["/foreign-write-root"]),
            ("flags", ["--add-dir", "/foreign-write-root"]),
            ("options", ["--add-dir", "/foreign-write-root"]),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "launch fields"):
                tui_plan.decode(data | {key: value})

    def test_wire_validates_each_command_component_and_prompt(self):
        data = json.loads(tui_plan.encode(self.plan))
        for key in ("selection", "hook"):
            for value in (None, "word", [1], ["a\0b"]):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ValueError, "invalid command"),
                ):
                    tui_plan.decode(data | {key: value})
        with self.assertRaisesRegex(ValueError, "invalid command"):
            tui_plan.decode(data | {"hook": []})
        for value in (None, 1, "relative", "/a\0b"):
            with (
                self.subTest(executable=value),
                self.assertRaisesRegex(ValueError, "absolute executables"),
            ):
                tui_plan.decode(data | {"executable": value})
        for word in ("--add-dir", "-c", "--", "-s", "--sandbox=danger-full-access"):
            with self.subTest(selection=word), self.assertRaisesRegex(ValueError, "selection"):
                tui_plan.decode(data | {"selection": ["resume", "saved-native", word]})
        for value in (None, 1, "a\0b"):
            with self.subTest(prompt=value), self.assertRaisesRegex(ValueError, "invalid prompt"):
                tui_plan.decode(data | {"prompt": value})

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
        hook = tui_plan.hook_plan(argv, tuple(roots))
        self.assertEqual(hook.argv, argv)
        self.assertEqual(hook.import_roots, tuple(roots))
        decoded = tui_plan.decode(
            json.loads(tui_plan.encode(dataclasses.replace(self.plan, hook=hook)))
        )
        self.assertEqual(decoded.hook, hook)
        commands = tui_plan.commands(hook)
        self.assertEqual(list(commands), [event for event, _, _ in HOOKS])
        flags = tui_plan.hook_flags(hook)
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

    def test_execution_and_trust_use_the_same_pre_separator_flags(self):
        hooks = tui_plan.hook_flags(self.plan.hook)
        trusted = "hooks.state={trusted}"
        command = tui_plan.execution_argv(self.plan, self.snapshot, trusted)
        self.assertEqual(
            command,
            [
                *self.frontend,
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "-m",
                "configured",
                "--add-dir",
                "/granted",
                *hooks,
                "-c",
                trusted,
                "--",
                "--config=literal",
                "--disable",
                "literal",
                self.plan.prompt,
            ],
        )
        self.assertEqual(
            tui_plan.trust_args(self.plan, self.snapshot),
            [*hooks, "-c", 'model_verbosity="low"', "--enable", "web"],
        )
        changed = dataclasses.replace(self.snapshot, flags=("--disable", "web"))
        self.assertEqual(tui_plan.trust_args(self.plan, changed), [*hooks, "--disable", "web"])
        changed_command = tui_plan.execution_argv(self.plan, changed, trusted)
        self.assertEqual(changed_command[len(self.frontend) :][:2], ["--disable", "web"])
        self.assertNotIn("--enable", changed_command)

    def test_empty_flags_and_prompt_preserve_the_literal_tail_contract(self):
        for flags, prompt, tail in (
            ((), "", []),
            ((), "hello", ["--", "hello"]),
            (("--",), "", ["--"]),
            (("--",), "hello", ["--", "hello"]),
            (("--", "--config=literal"), "", ["--", "--config=literal"]),
            (("--", "--config=literal"), "hello", ["--", "--config=literal", "hello"]),
        ):
            plan = dataclasses.replace(self.plan, prompt=prompt)
            snapshot = dataclasses.replace(self.snapshot, flags=flags)
            with self.subTest(flags=flags, prompt=prompt):
                self.assertEqual(
                    tui_plan.execution_argv(plan, snapshot, "trusted"),
                    [
                        *self.frontend,
                        "-m",
                        "configured",
                        "--add-dir",
                        "/granted",
                        *tui_plan.hook_flags(plan.hook),
                        "-c",
                        "trusted",
                        *tail,
                    ],
                )
                self.assertEqual(
                    tui_plan.trust_args(plan, snapshot), list(tui_plan.hook_flags(plan.hook))
                )

    def test_execution_write_grants_come_from_the_saved_snapshot(self):
        encoded = tui_plan.encode(self.plan)
        command = tui_plan.execution_argv(
            tui_plan.decode(json.loads(encoded)), self.snapshot, "trusted"
        )
        self.assertEqual(command[command.index("--add-dir") :][:2], ["--add-dir", "/granted"])
        self.assertNotIn("/foreign-write-root", command)
        changed = dataclasses.replace(self.snapshot, dirs=("/different-saved-grant",))
        changed_command = tui_plan.execution_argv(self.plan, changed, "trusted")
        self.assertEqual(
            changed_command[changed_command.index("--add-dir") :][:2],
            ["--add-dir", "/different-saved-grant"],
        )
        self.assertNotIn("/granted", changed_command)
        self.assertEqual(tui_plan.encode(self.plan), encoded)

    def test_standard_session_selectors_roundtrip_and_execute(self):
        for selection in ((), ("resume", "saved-native")):
            plan = dataclasses.replace(self.plan, selection=selection)
            decoded = tui_plan.decode(json.loads(tui_plan.encode(plan)))
            with self.subTest(selection=selection):
                self.assertEqual(decoded.selection, selection)
                command = tui_plan.execution_argv(decoded, self.snapshot, "trusted")
                self.assertEqual(command[: 1 + len(selection)], [plan.executable, *selection])
                self.assertEqual(
                    command[1 + len(selection) :][:5],
                    ["--no-daemon", "-C", "/work", "-c", 'tui.notification_method="bel"'],
                )

    def test_fixed_frontend_uses_the_saved_snapshot_cwd(self):
        snapshot = dataclasses.replace(self.snapshot, cwd="/work/restored-project")
        command = tui_plan.execution_argv(self.plan, snapshot, "trusted")
        self.assertEqual(command[command.index("-C") :][:2], ["-C", snapshot.cwd])
        self.assertNotIn("/work", command)
