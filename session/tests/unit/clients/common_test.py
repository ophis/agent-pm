from dataclasses import replace

from agent_pm.session.clients.common import check_selection, container, scan, split_flags
from agent_pm.session.engine.types import (
    LOCAL_CONTEXT,
    ClientContext,
    ConfigError,
    Container,
    Permission,
)

from session.tests.case import Case


class FlagsTest(Case):
    def test_unvalued_short_options_retain_their_raw_spelling(self):
        for arg in ("-x", "-xattached", "-x=value", "-x="):
            with self.subTest(arg=arg):
                flag = next(scan((arg,), {"-c": "-c"}))
                self.assertEqual(flag.word, arg[:2])
                self.assertIsNone(flag.value)
                self.assertEqual((flag.arg, flag.text), (arg, arg))

    def test_aliases_and_attached_or_following_values(self):
        valued = {"-c": "-c", "--config": "-c", "--add-dir": "--add-dir"}
        flags = ("-c", "one=1", "-ctwo=2", "-c=three=3", "--config=four=4", "--add-dir", "/x")
        self.assertEqual(
            [(flag.word, flag.value, flag.text) for flag in scan(flags, valued)],
            [
                ("-c", "one=1", "-c one=1"),
                ("-c", "two=2", "-ctwo=2"),
                ("-c", "three=3", "-c=three=3"),
                ("-c", "four=4", "--config=four=4"),
                ("--add-dir", "/x", "--add-dir /x"),
            ],
        )

    def test_consumed_spans_remove_only_the_selected_option(self):
        for settings in (("--settings", "{}"), ("--settings={}",)):
            flags = ("--verbose", *settings, "--other", "--", "literal")
            with self.subTest(settings=settings):
                option = next(
                    flag
                    for flag in scan(flags, {"--settings": "--settings"})
                    if flag.word == "--settings"
                )
                self.assertEqual(option.value, "{}")
                self.assertEqual(
                    (*flags[: option.start], *flags[option.end :]),
                    ("--verbose", "--other", "--", "literal"),
                )

    def test_empty_values_do_not_consume_another_option(self):
        valued = {"-c": "-c", "--config": "-c"}
        for flags in (("-c=", "--other"), ("--config=", "--other")):
            with self.subTest(flags=flags):
                self.assertEqual(
                    [(flag.word, flag.value) for flag in scan(flags, valued)],
                    [("-c", ""), ("--other", None)],
                )
        self.assertEqual(next(scan(("-c",), valued)).text, "-c ")

    def test_a_next_value_may_be_a_flag_or_the_separator(self):
        valued = {"--add-dir": "--add-dir"}
        for value in ("--verbose", "--"):
            flags = ("--add-dir", value, "--other")
            with self.subTest(value=value):
                self.assertEqual(
                    [(flag.word, flag.value) for flag in scan(flags, valued)],
                    [("--add-dir", value), ("--other", None)],
                )
        self.assertEqual(
            split_flags(("--add-dir", "--", "--other")), (["--add-dir"], ["--", "--other"])
        )

    def test_the_literal_tail_starts_at_the_first_separator(self):
        flags = ("--verbose", "--", "--resume", "--", "prompt")
        self.assertEqual([flag.arg for flag in scan(flags)], ["--verbose"])
        self.assertEqual(split_flags(flags), (["--verbose"], ["--", "--resume", "--", "prompt"]))
        for words, expected in (
            ((), ([], [])),
            (("--", "literal"), ([], ["--", "literal"])),
            (("--verbose",), (["--verbose"], [])),
            (("--verbose", "--"), (["--verbose"], ["--"])),
        ):
            with self.subTest(words=words):
                self.assertEqual(split_flags(words), expected)

    def test_selection_retains_short_names_and_skips_option_values(self):
        for flag in ("-r", "-rsession", "-r=session", "--resume=session"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ConfigError, "picks the session"):
                check_selection((flag,), ("-r", "--resume"))
        check_selection(("--model", "--resume", "--", "-r"), ("-r", "--resume"), ("--model",))


class ContainerTest(Case):
    CONTEXT = ClientContext(
        "host.docker.internal",
        Permission.YOLO,
        bin_dir="/opt/agent-pm/bin",
        python="/opt/agent-pm/venv/bin/python",
        pythonpath="/opt/agent-pm/session/src:/opt/agent-pm/utils/src",
        state_root="/run/agent-pm/state",
    )

    def test_a_local_context_has_no_container(self):
        self.assertIsNone(container(LOCAL_CONTEXT))

    def test_a_container_context_gives_its_paths_and_image_source_root(self):
        found = container(self.CONTEXT)
        self.assertEqual(
            found,
            Container(
                "/opt/agent-pm/bin",
                "/opt/agent-pm/venv/bin/python",
                "/opt/agent-pm/session/src:/opt/agent-pm/utils/src",
                "/run/agent-pm/state",
            ),
        )
        self.assertEqual(found.source, "/opt/agent-pm/session/src")

    def test_a_bin_dir_without_the_other_paths_is_refused(self):
        for field in ("python", "pythonpath", "state_root"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ConfigError, "bin_dir without python"),
            ):
                container(replace(self.CONTEXT, **{field: None}))
