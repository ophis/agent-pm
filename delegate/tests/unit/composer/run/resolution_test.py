"""Resolution behavior."""

import inspect
import os
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import composer
from agent_pm.delegate.composer import (
    ConfigError,
    ConfigSource,
)
from agent_pm.delegate.composer.run import (
    RUN_KEYS,
    check_old_keys,
    resolve,
    trusted_dirs,
)
from agent_pm.utils import read_project

from delegate.tests.unit.composer.run.case import RUNTIME, TIER, Fake
from delegate.tests.unit.composer.teams import EDITOR, POET, RUN_CONFIG, VERSE


class Resolve(Fake):
    def test_role_overrides_global_output_is_replaced_whole_and_lists_default_empty(self):
        for role, tier, effort, output in (
            ("editor", 1, "high", {"type": "github", "repo": "o/docs", "branch": "main"}),
            ("writer", 2, "medium", {"type": "local"}),
        ):
            with self.subTest(role=role):
                run = self.resolved(role)
                self.assertEqual((run.tier, run.effort, run.output), (tier, effort, output))
        run = self.resolved("editor")
        self.assertEqual((run.read, run.write, run.commands, run.templates), ([], [], [], []))
        self.assertEqual(run.role_title, "Editor")

    def test_a_task_is_the_named_one_else_empty(self):
        self.assertEqual(self.resolved().task, "")
        self.assertEqual(self.resolved(task="long-note").task, "long-note")
        self.assertEqual(self.resolved("editor", "long-note").task, "long-note")

    def test_later_overlays_replace_run_keys_at_any_level(self):
        overlay = {"effort": "low", "roles": {"editor": {"tier": 3, "output": {"type": "local"}}}}
        run = self.resolved("editor", overlays=[overlay])
        self.assertEqual((run.tier, run.effort, run.output), (3, "low", {"type": "local"}))
        self.assertEqual(self.resolved("editor", overlays=[overlay, {"tier": 4}]).tier, 4)

    def test_gate_defaults_to_empty_and_an_overlays_or_the_configs_gate_is_its_value_verbatim(self):
        self.assertEqual(self.resolved().gate, "")
        gate = "python3 /u/usage.py --below 80 *"
        self.assertEqual(self.resolved(overlays=[{"roles": {"writer": {"gate": gate}}}]).gate, gate)
        self.configure(
            RUN_CONFIG.replace('effort = "medium"', 'effort = "medium"\ngate = "make gate"')
        )
        self.assertEqual(self.resolved().gate, "make gate")

    def test_language_at_any_level(self):
        self.assertEqual(self.resolved("editor").language, "")
        self.configure(
            RUN_CONFIG.replace('effort = "high"', 'effort = "high"\nlanguage = "French"')
        )
        self.assertEqual(self.resolved("editor").language, "French")
        overlay = {"roles": {"editor": {"language": "German"}}}
        self.assertEqual(self.resolved("editor", overlays=[overlay]).language, "German")
        self.assertEqual(self.resolved("editor", overlays=[{"language": ""}]).language, "")

    def test_the_local_file_goes_on_top(self):
        self.local(
            'language = "French"\nshow = ""\ncwd = "/srv"\n'
            '[roles.editor]\ntier = 3\n[roles.editor.output]\nbranch = "trunk"\n'
            '[roles.writer]\nread = ["/data"]\n'
        )
        run = self.resolved("editor")
        self.assertEqual(
            (run.language, run.show, run.cwd, run.tier, run.effort),
            ("French", "", "/srv", 3, "high"),
        )
        self.assertEqual(run.output, {"type": "github", "repo": "o/docs", "branch": "trunk"})
        self.assertEqual(self.resolved().read, ["/data"])


class Overlays(Fake):
    def test_the_config_with_the_local_file_then_each_overlay_in_order(self):
        self.local('language = "French"\n[roles.writer]\neffort = "max"\n')
        run = self.resolved(
            "writer",
            "long-note",
            overlays=(
                {"tier": 4, "effort": "high", "output": {"type": "orchestrator"}},
                {"roles": {"writer": {"tier": 1, "output": {"type": "local"}}}},
            ),
        )
        self.assertEqual(
            (run.tier, run.effort, run.language, run.task, run.output),
            (1, "high", "French", "long-note", {"type": "local"}),
        )
        run = self.resolved(overlays=({"output": {"type": "orchestrator"}},))
        self.assertEqual((run.effort, run.output), ("max", {"type": "orchestrator"}))
        self.assertEqual(self.resolved(overlays=({},)), self.resolved())

    def test_an_overlay_rejects_unknown_and_legacy_keys(self):
        bad = (
            ({"flags": []}, "flags"),
            ({"tasks": {}}, "tasks"),
            ({"output": {"type": "local", "typo": "value"}}, "typo"),
            ({"roles": {"writer": {"default_task": "short-note"}}}, "default_task"),
            ({"roles": {"writer": {"tasks": {}}}}, "tasks"),
            ({"roles": {"writer": {"allow": []}}}, "allow"),
            ({"roles": {"writer": {"output": {"type": "local", "typo": "value"}}}}, "typo"),
            ({"roles": {"stranger": {"tier": 3}}}, "stranger"),
        )
        for overlay, message in bad:
            with self.subTest(overlay=overlay), self.assertRaisesRegex(ConfigError, message):
                self.resolved(overlays=(overlay,))

    def test_malformed_overlay_tables_fail_clearly(self):
        for overlay in (
            None,
            [],
            {"roles": []},
            {"roles": {"writer": 2}},
            {"output": []},
            {"roles": {"writer": {"output": []}}},
        ):
            with self.subTest(overlay=overlay), self.assertRaises(ConfigError):
                self.resolved(overlays=(overlay,))

    def test_an_unknown_role(self):
        self.assertEqual(self.refused("stranger"), "unknown role 'stranger'")

    def test_non_string_overlay_key_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self.resolved(overlays=({1: "bad", "typo": "bad"},))

    def test_overlay_errors_name_the_label_source_and_table(self):
        bad = (
            ({"foo": 1}, "unknown key 'foo' in [{t}]"),
            ({"roles": {"stranger": {"tier": 3}}}, "unknown role 'stranger' in [{t}.roles]"),
            ({"roles": {"writer": {"foo": 1}}}, "unknown key 'foo' in [{t}.roles.writer]"),
            (
                {"roles": {"writer": {"output": {"type": "local", "typo": 1}}}},
                "unknown key 'typo' in [{t}.roles.writer.output]",
            ),
            ({"output": {"type": "local", "typo": 1}}, "unknown key 'typo' in [{t}.output]"),
            (
                {"roles": {"writer": {"tasks": {}}}},
                "[{t}.roles.writer] has 'tasks': task-level config and "
                "default_task are gone; set run keys in [{t}.roles.writer] (a run without a task picks one)",
            ),
        )
        labels = (
            ({}, "run overlay", "overlay"),
            ({"label": ("my.toml", "core")}, "my.toml", "core"),
        )
        for (overlay, message), (kwargs, source, table) in (
            (b, label) for b in bad for label in labels
        ):
            with self.subTest(overlay=overlay, table=table):
                self.assertEqual(
                    self.refused(overlays=(overlay,), **kwargs),
                    f"{source}: {message.format(t=table)}",
                )


class OldKeys(Fake):
    def test_task_tables_or_default_task_in_any_role_table(self):
        for local in (
            '[roles.writer]\ndefault_task = "short-note"\n',
            "[roles.writer.tasks.short-note]\ntier = 3\n",
            '[roles.writer]\ndefault_task = "x"\nmodel = "y"\n',
        ):
            for role in ("writer", "editor"):
                with self.subTest(local=local, role=role):
                    self.local(local)
                    msg = self.refused(role)
                    self.assertIn("[roles.writer]", msg)
                    self.assertNotIn("unknown key", msg)

    def test_the_check_names_its_layers_table(self):
        for table in ({"default_task": "x"}, {"tasks": {}}, {"tier": 1, "tasks": {"t": {}}}):
            with self.subTest(table=table), self.assertRaises(ConfigError) as cm:
                check_old_keys(table, "researcher", "core.")
            self.assertIn("[core.roles.researcher]", str(cm.exception))
        check_old_keys({"tier": 1, "output": {}}, "researcher")


class Validate(Fake):
    def test_an_invalid_config_fails_whatever_the_overlays(self):
        for old, new, msg, role, overlays in (
            ("tier = 1", "tier = 9", TIER, "editor", [{"tier": 2}]),
            ("tier = 1", "tier = 5", TIER, "editor", ()),
            ('effort = "medium"', 'effort = "ultra"', "effort must be one of", "writer", ()),
        ):
            with self.subTest(new=new):
                self.configure(RUN_CONFIG.replace(old, new))
                self.assertIn(msg, self.refused(role, overlays=overlays))

    def test_the_local_file_is_checked_with_the_config(self):
        self.local('[roles.editor]\nmodel = "opus"\n')
        self.assertEqual(self.refused("editor"), "unknown key 'model' in roles.editor")

    def test_config_errors_come_before_a_missing_charter_or_task_file(self):
        self.configure(RUN_CONFIG.replace("tier = 1", "tier = 9"))
        os.remove(os.path.join(self.root, "tasks", "long-note.md"))
        os.remove(os.path.join(self.root, "roles", "editor.md"))
        self.assertIn(TIER, self.refused("editor"))

    def test_a_configured_role_without_its_charter(self):
        os.remove(os.path.join(self.root, "roles", "editor.md"))
        self.assertEqual(
            self.refused("editor"), "roles.editor: no charter roles/editor.md in core or team_dirs"
        )

    def test_an_invalid_overlay_value(self):
        self.assertLessEqual({"language", "show", "runtime"}, RUN_KEYS)
        gate, language, show = (
            "gate must be one line of shell command without backticks",
            "language must be one line of text",
            "show must be one line of shell command",
        )
        for overlay, msg in (
            ({"tier": 9}, TIER),
            ({"gate": "a\nb"}, gate),
            ({"gate": "echo `id`"}, gate),
            ({"gate": 7}, gate),
            ({"language": "a\nb"}, language),
            ({"language": 7}, language),
            ({"show": "a\nb"}, show),
            ({"show": 7}, show),
        ):
            with self.subTest(overlay=overlay):
                self.assertIn(msg, self.refused("editor", overlays=[overlay]))

    def test_replace_is_checked(self):
        run = self.resolved("editor")
        for change in ({"effort": "ultra"}, {"gate": "a\nb"}, {"runtime": ""}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                replace(run, **change)

    def test_show_is_empty_a_pane_by_default_and_an_overlay_sets_it_at_any_level(self):
        self.assertEqual(self.resolved("editor").show, "")
        for overlay, show in (
            ({"show": "none"}, "none"),
            ({"show": ""}, ""),
            ({"show": "open-it {{session}}"}, "open-it {{session}}"),
            ({"roles": {"editor": {"show": "x {{session}}"}}}, "x {{session}}"),
        ):
            with self.subTest(overlay=overlay):
                self.assertEqual(self.resolved("editor", overlays=[overlay]).show, show)

    def test_unknown_role_or_task_or_another_roles(self):
        for role, task, msg in (
            ("nobody", None, "unknown role 'nobody'"),
            (
                "writer",
                "essay",
                "task 'essay' is not one of writer's tasks (short-note, long-note)",
            ),
            ("editor", "short-note", "task 'short-note' is not one of editor's tasks (long-note)"),
        ):
            with self.subTest(role=role, task=task):
                self.assertEqual(self.refused(role, task), msg)


class OwnedKeys(Fake):
    def test_another_layers_global_keys_are_left_alone(self):
        self.configure(
            "surprise = 1\n"
            + RUN_CONFIG
            + '\n[adapters.x]\nimplementation = "adapters.x:XAdapter"\nmodel = "m"\n'
        )
        self.assertEqual(self.resolved().runtime, "a")

    def test_owned_tables_and_overlays_stay_strict(self):
        for local, overlay, msg in (
            ('[roles.writer]\nmodel = "m"\n', {}, "unknown key 'model' in roles.writer"),
            ('[roles.writer]\nteam_dirs = ["/x"]\n', {}, "unknown key 'team_dirs' in roles.writer"),
            ("[output]\ntypo = 1\n", {}, "unknown key 'typo' in output"),
            ("[roles.editor.output]\ntypo = 1\n", {}, "unknown key 'typo' in roles.editor.output"),
            ('output = "local"\n', {}, "output must be a table"),
            ('[roles.writer]\noutput = "local"\n', {}, "roles.writer.output must be a table"),
            ("", {"model": "m"}, "run overlay: unknown key 'model' in [overlay]"),
            (
                "",
                {"roles": {"writer": {"model": "m"}}},
                "run overlay: unknown key 'model' in [overlay.roles.writer]",
            ),
        ):
            with self.subTest(msg=msg):
                self.local(local)
                self.assertEqual(self.refused(overlays=(overlay,)), msg)


class Runtime(Fake):
    def setUp(self):
        super().setUp()
        for role in ("pm", "researcher"):
            self.write({f"roles/{role}.md": EDITOR.replace("# Editor", f"# {role}")})

    def test_the_configs_value_and_a_role_overlay_per_role(self):
        overlay = {"roles": {"pm": {"runtime": "x"}}}
        self.assertEqual(
            [self.resolved(role, overlays=(overlay,)).runtime for role in ("pm", "researcher")],
            ["x", "a"],
        )

    def test_the_configs_role_table_per_role(self):
        self.configure(RUN_CONFIG + '\n[roles.pm]\nruntime = "x"\n')
        self.assertEqual([self.resolved(role).runtime for role in ("pm", "researcher")], ["x", "a"])

    def test_an_overlays_global_value_beats_the_configs_role_value(self):
        self.configure(RUN_CONFIG.replace('effort = "medium"', 'effort = "medium"\nruntime = "w"'))
        self.assertEqual(self.resolved().runtime, "w")
        self.assertEqual(self.resolved(overlays=({"runtime": "x"},)).runtime, "x")

    def test_missing_or_not_a_one_line_name_after_any_layer(self):
        unset = RUN_CONFIG.replace('runtime = "a"\n', "")
        for config, overlays, got in (
            (unset, (), "''"),
            (RUN_CONFIG.replace('"a"', '""'), (), "''"),
            (RUN_CONFIG.replace('"a"', '"  "'), (), "'  '"),
            (RUN_CONFIG.replace('"a"', '"a\\nb"'), (), "'a\\nb'"),
            (RUN_CONFIG.replace('"a"', "3"), (), "3"),
            (RUN_CONFIG, ({"runtime": ""},), "''"),
            (RUN_CONFIG, ({"roles": {"writer": {"runtime": ["x"]}}},), "['x']"),
        ):
            with self.subTest(got=got, overlays=overlays):
                self.configure(config)
                self.assertEqual(self.refused(overlays=overlays), RUNTIME + got)
        self.local('runtime = "l"\n')
        self.assertEqual(self.resolved().runtime, "l")

    def test_resolve_takes_no_runtime_client_or_trust(self):
        self.assertEqual(
            list(inspect.signature(resolve).parameters),
            ["role", "task", "overrides", "team", "source"],
        )

    def test_a_read_config_given_is_used_reading_no_file(self):
        cfg = read_project("delegate")
        want = (resolve("pm"), trusted_dirs(), composer.index("pm"))
        original = open

        def opened(path, *args, **kwargs):
            if str(path).endswith(".toml"):
                raise OSError("read")
            return original(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=opened):
            got = (
                resolve("pm", source=ConfigSource(table=cfg)),
                trusted_dirs(cfg=cfg),
                composer.index("pm", cfg=cfg),
            )
        self.assertEqual(got, want)


class TeamRoles(Fake):
    def test_a_team_dirs_role_without_a_table_takes_the_global_keys_and_an_overlay(self):
        found, _ = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        run = self.resolved(
            "poet", "verse", found, overlays=({"roles": {"poet": {"effort": "low"}}},)
        )
        self.assertEqual(
            (run.role, run.task, run.tier, run.effort, run.output, run.templates, run.role_title),
            ("poet", "verse", 2, "low", {"type": "local"}, [], "Poet"),
        )
        self.assertEqual(self.refused("poet"), "unknown role 'poet'")
        self.assertEqual(
            self.refused(task="verse", found=found),
            "task 'verse' is not one of writer's tasks (short-note, long-note)",
        )

    def test_a_misnamed_file_a_run_names_or_a_role_table_without_a_charter(self):
        found, top = self.team_dir({"roles/Poet.md": POET, "tasks/Verse.md": VERSE})
        self.assertEqual(
            self.refused("Poet", found=found), f"{top}/roles/Poet.md: name 'Poet': want [a-z0-9-]+"
        )
        self.assertEqual(
            self.refused(task="Verse", found=found),
            f"{top}/tasks/Verse.md: name 'Verse': want [a-z0-9-]+",
        )
        self.local("[roles.ghost]\ntier = 3\n")
        self.assertEqual(
            self.refused("ghost"), "roles.ghost: no charter roles/ghost.md in core or team_dirs"
        )
