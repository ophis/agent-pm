"""request test behavior."""

import dataclasses
from types import MappingProxyType

from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    Spec,
)

from session.tests.case import Case
from session.tests.unit.adapters.base_fixtures import (
    Refusals,
)


class AccessTest(Refusals):
    def test_defaults(self):
        access = Access()
        self.assertEqual(
            (access.dirs, access.commands, access.cwd, access.project, access.reads, access.env),
            ((), (), "", False, (), {}),
        )

    def test_accepts_tuples_of_strings(self):
        access = Access(dirs=("/a",), commands=("git",), cwd="/c", project=True, reads=("/r",))
        self.assertEqual((access.dirs, access.commands, access.reads), (("/a",), ("git",), ("/r",)))

    def test_keeps_reads_and_env_together(self):
        access = Access(reads=("/r",), env={"A": "b"})
        self.assertEqual((access.reads, access.env), (("/r",), {"A": "b"}))

    def test_takes_keywords_only(self):
        with self.assertRaises(TypeError):
            Access(("/a",))

    def test_refuses_dirs_commands_or_reads_that_are_not_a_tuple_of_strings(self):
        for bad in (["/a"], ("/a", 1), "/a", None):
            with self.subTest(bad=bad):
                self.refused(lambda *, bad=bad: Access(dirs=bad), "dirs must be a tuple of strings")
                self.refused(
                    lambda *, bad=bad: Access(commands=bad), "commands must be a tuple of strings"
                )
                self.refused(
                    lambda *, bad=bad: Access(reads=bad), "reads must be a tuple of strings"
                )

    def test_accepts_an_env_of_shell_names_to_strings(self):
        env = {"PYTHONPATH": "/a b;c", "_x1": ""}
        self.assertEqual(Access(env=MappingProxyType(env)).env, env)

    def test_refuses_an_env_name_a_shell_assignment_cannot_hold_or_a_value_not_a_string_without_nul(
        self,
    ):
        message = "access env must map names ([A-Za-z_][A-Za-z0-9_]*) to strings without NUL"
        for bad in (
            {"X;Y": "a"},
            {"A=B": "a"},
            {"1A": "a"},
            {"": "a"},
            {"A B": "a"},
            {"A\n": "a"},
            {1: "a"},
            {"A": 1},
            {"A": None},
            {"A": "b\0"},
            [("A", "b")],
            None,
        ):
            with self.subTest(bad=bad):
                self.refused(lambda *, bad=bad: Access(env=bad), message)


class SpecTest(Refusals):
    def test_defaults(self):
        spec = Spec(workdir="/w")
        self.assertEqual(
            (spec.access, spec.tier, spec.effort, spec.sid), (Access(), None, None, None)
        )
        self.assertEqual(
            (spec.flags, spec.env, spec.name, spec.protect, spec.reply_tags),
            ((), {}, None, (), None),
        )

    def plain(self, **fields):
        return Spec(
            workdir="/w",
            flags=("--model", "x", "--", "-p"),
            env={"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
            name="worker-1",
            protect=("/p", "/q r/ü"),
            **fields,
        )

    def test_accepts_the_plain_session_fields(self):
        spec = self.plain()
        self.assertEqual(
            (spec.flags, dict(spec.env), spec.name, spec.protect),
            (
                ("--model", "x", "--", "-p"),
                {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
                "worker-1",
                ("/p", "/q r/ü"),
            ),
        )
        self.assertEqual(Spec(workdir="/w", env=MappingProxyType({"A": "1"})).env, {"A": "1"})
        self.assertEqual(Spec(workdir="/w", name="").name, "")

    def test_env_is_an_immutable_copy_and_specs_compare_by_value(self):
        source = {"A": "1"}
        spec = Spec(workdir="/w", env=source)
        source["B"] = "2"
        self.assertEqual(dict(spec.env), {"A": "1"})
        with self.assertRaises(TypeError):
            spec.env["C"] = "3"
        self.assertEqual(spec, Spec(workdir="/w", env={"A": "1"}))
        self.assertNotEqual(spec, Spec(workdir="/w", env={"A": "2"}))
        self.assertEqual(self.plain(), self.plain())

    def test_replace_keeps_the_plain_session_fields(self):
        spec = dataclasses.replace(self.plain(), tier=2, sid="s")
        self.assertEqual(spec, self.plain(tier=2, sid="s"))
        self.assertEqual(dict(spec.env), {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"})

    def test_refuses_flags_that_are_not_a_tuple_of_strings_without_nul(self):
        for bad in (["-x"], ("-x\0",), ("-x", 1), "-x", None):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", flags=bad),
                    "flags must be a tuple of strings without NUL",
                )

    def test_refuses_env_that_does_not_map_variable_names_to_strings_without_nul(self):
        for bad in (
            {"A": 1},
            {"A": None},
            {"A": "b\0"},
            {1: "a"},
            {"": "a"},
            {"1A": "a"},
            {"A-B": "a"},
            {"A B": "a"},
            {"A=": "a"},
            {"A\n": "a"},
            {"A\0": "a"},
            {"Ä": "a"},
            [("A", "b")],
            "A=b",
            None,
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", env=bad),
                    "env must map variable names to strings without NUL",
                )

    def test_refuses_a_name_other_than_none_or_a_string(self):
        for bad in (5, b"w", ("w",)):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", name=bad), "name must be None or a string"
                )

    def test_refuses_protect_that_is_not_a_tuple_of_printable_absolute_paths(self):
        for bad in (["/p"], ("p",), ("",), ("/p\n",), ("/p\0",), ("/p", 1), (b"/p",), "/p", None):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", protect=bad),
                    "protect must be a tuple of printable absolute paths",
                )

    def test_reply_tags_are_none_or_a_progress_and_an_outcome_tag(self):
        self.assertEqual(Spec(workdir="/w", reply_tags=("p-1", "o_2")).reply_tags, ("p-1", "o_2"))
        for bad in (
            ["p", "o"],
            ("p",),
            ("p", "o", "x"),
            ("p", ""),
            ("p", "o b"),
            ("p:", "o"),
            ("p", 1),
            "po",
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", reply_tags=bad),
                    "reply_tags must be None or (progress tag, outcome tag), each [A-Za-z0-9_-]+",
                )

    def test_keyword_only_and_frozen(self):
        with self.assertRaises(TypeError):
            Spec("/w")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Spec(workdir="/w").sid = "s"

    def test_accepts_every_tier_and_effort(self):
        self.assertEqual(engine_types.EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        for tier in (1, 2, 3, 4):
            for effort in engine_types.EFFORTS:
                spec = Spec(
                    workdir="/w", access=Access(cwd="/c"), tier=tier, effort=effort, sid="s-1"
                )
                self.assertEqual((spec.tier, spec.effort), (tier, effort))

    def test_refuses_a_workdir_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (
            ("w", "'w'"),
            ("", "''"),
            (5, "5"),
            (None, "None"),
            ("/w\n", r"'/w\n'"),
            ("/w\0", r"'/w\x00'"),
            (b"/w", "b'/w'"),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir=bad),
                    f"workdir must be a printable absolute path, got {shown}",
                )

    def test_shows_at_most_80_characters_of_a_bad_workdir(self):
        self.refused(
            lambda: Spec(workdir="/" + "x" * 200 + "\n"),
            "workdir must be a printable absolute path, got '/" + "x" * 78,
        )

    def test_refuses_a_tier_other_than_an_integer_1_to_4(self):
        for bad, shown in ((0, "0"), (5, "5"), (True, "True"), ("2", "'2'"), (2.0, "2.0")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", tier=bad),
                    f"tier must be an integer 1–4, got {shown}",
                )

    def test_refuses_an_effort_outside_efforts(self):
        for bad, shown in (("extreme", "'extreme'"), ("", "''"), ("LOW", "'LOW'"), (3, "3")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", effort=bad),
                    f"effort must be one of low, medium, high, xhigh, max, got {shown}",
                )

    def test_refuses_an_access_cwd_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (("rel", "'rel'"), ("/c\0", r"'/c\x00'")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", access=Access(cwd=bad)),
                    f"cwd must be a printable absolute path, got {shown}",
                )

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", sid=bad),
                    f'sid must be a non-empty printable string without "/", got {shown}',
                )


class SidErrorTest(Case):
    def test_none_for_a_non_empty_printable_string_without_a_slash(self):
        for sid in ("s", "s-1", "0b6c8f9e-2a1d-4c3e-9f7a-5b4d3c2e1f00", "a b", "ü"):
            with self.subTest(sid=sid):
                self.assertIsNone(engine_types.sid_error(sid))

    def test_the_error_text_otherwise(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            (None, "None"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    engine_types.sid_error(bad),
                    f'sid must be a non-empty printable string without "/", got {shown}',
                )


class LaunchTest(Refusals):
    def test_defaults(self):
        launch = Launch(("x",))
        self.assertEqual(
            (launch.argv, launch.env, launch.unset, launch.cwd, launch.transcript, launch.resume),
            (("x",), {}, (), "", "", ""),
        )

    def test_project_override_is_optional_and_boolean(self):
        self.assertIsNone(Launch(("x",)).project)
        for value in (False, True):
            self.assertIs(Launch(("x",), project=value).project, value)
        for value in (0, 1, "yes", []):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "launch: project"):
                Launch(("x",), project=value)

    def test_accepts_strings(self):
        launch = Launch(
            ("x", "-y"),
            env=MappingProxyType({"A": "b"}),
            unset=("C",),
            cwd="/c",
            transcript="/t",
            resume="x --resume s",
        )
        self.assertEqual((launch.env["A"], launch.unset), ("b", ("C",)))

    def test_refuses_argv_env_or_unset_that_is_not_strings_without_nul(self):
        for field, bads in (
            ("argv", (["x"], ("x\0",), ("x", 1), "x")),
            ("env", ({"A": 1}, {1: "a"}, {"A\0": "b"}, {"A": "b\0"}, [("A", "b")])),
            ("unset", (["A"], ("A\0",), ("A", 2))),
        ):
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    fields = {"argv": ("x",), field: bad}
                    self.refused(
                        lambda *, fields=fields: Launch(**fields),
                        f"launch: {field} must be strings without NUL",
                    )
