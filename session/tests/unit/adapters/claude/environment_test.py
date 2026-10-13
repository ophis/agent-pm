"""environment test behavior."""

import os
import shlex

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.common.types import ClaudeCommon as UnconfiguredCommon
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import (
    Access,
    Launch,
    SessionRequest,
    SessionStatus,
    Usage,
    Window,
)

from session.tests.unit.adapters.claude.fixtures import (
    CEILING,
    CONFIG,
    HOOK,
    NOTE,
    PROBE,
    SID,
    UNSET,
    Base,
    ClaudeCommon,
    Spec,
    event,
    tui_launch,
)


class ReadRulesTest(Base):
    def modes(self, *, resume=False, config=None, **access):
        spec = Spec(workdir=self.work, sid=SID, access=Access(**access))
        adapter = ClaudeAdapter({} if config is None else config)
        return (
            lambda: adapter.headless().launch("hi", SessionRequest(spec, resumed=resume)),
            lambda: adapter.tui().launch("hi", SessionRequest(spec, resumed=resume), HOOK),
        )

    def both(self, *, resume=False, config=None, **access) -> list[tuple[str, ...]]:
        return [launch().argv for launch in self.modes(resume=resume, config=config, **access)]

    def test_reads_alone_are_allowed_tools_never_added_dirs(self):
        for argv in self.both(reads=("/r",)):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//r/**)", "--", "hi"))
            self.assertNotIn("--add-dir", argv)

    def test_the_root_is_one_rule(self):
        for argv in self.both(reads=("/", "//")):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//**)", "--", "hi"))

    def test_a_symlinked_read_dir_gets_a_rule_for_both_spellings_each_once(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for reads, dirs in (
            ((link,), (link, target)),
            ((link + "/", target), (link, target)),
            ((target, link), (target, link)),
        ):
            with self.subTest(reads=reads):
                for argv in self.both(reads=reads):
                    self.assertEqual(
                        argv[-5:],
                        ("--allowedTools", *(f"Read(/{d}/**)" for d in dirs), "--", "hi"),
                    )

    def test_a_resume_keeps_the_rules(self):
        for argv in self.both(resume=True, config=CONFIG, commands=("ls",), reads=("/r",)):
            self.assertEqual(argv[1 + argv.index("--resume")], SID)
            self.assertEqual(
                argv[-7:],
                ("--allowedTools", "Bash(ls)", "Read(//r/**)", "Read", "Edit(/x)", "--", "hi"),
            )

    def test_a_read_dir_a_rule_cannot_hold_literally_is_refused_before_any_launch(self):
        bad = ["", "r", "./r", "/r\n", "/r\0", *(f"/r{c}s" for c in "*?[]{}()\\,"), "/" + "*" * 99]
        for read in bad:
            for launch in self.modes(reads=("/ok", read)):
                with self.subTest(read=read):
                    self.refused(
                        launch,
                        f"claude: read dir {read!r:.80} must be a printable absolute path "
                        "without *?[]{}()\\,",
                    )

    def test_a_read_dir_whose_real_path_a_rule_cannot_hold_is_refused(self):
        target = os.path.join(self.tmp, "a*b")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for launch in self.modes(reads=(link,)):
            self.refused(
                launch,
                f"claude: read dir {target!r:.80} must be a printable absolute path without "
                "*?[]{}()\\,",
            )


class EnvTest(Base):
    def test_headless_defaults_overlaid_by_the_config_env_tui_only_the_config_env(self):
        self.assertEqual(ClaudeCommon({}).env(), {})
        config = {"env": {"K": "v", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1"}}
        self.assertEqual(dict(self.launch(config=config).env), config["env"])
        self.assertEqual(dict(self.launch(config={}).env), CEILING)
        self.assertEqual(dict(tui_launch(self.work, config={}).env), {})

    def test_the_launch_env_unset_and_cwd(self):
        launch = self.launch()
        self.assertEqual(dict(launch.env), {**CEILING, "K": "v"})
        self.assertEqual(launch.unset, UNSET)
        self.assertEqual(ClaudeCommon.UNSET, UNSET)
        self.assertEqual(launch.cwd, "")

    def test_headless_uses_session_flags_and_env(self):
        flags = ("--permission-mode", "auto")
        launch = self.launch(flags=flags, env={"K": "session", "CUSTOM": "value"})
        self.assertEqual(launch.argv[-4:], (*flags, "--", "hi"))
        self.assertEqual(dict(launch.env), {**CEILING, "K": "session", "CUSTOM": "value"})
        self.assertIn("K=session CUSTOM=value claude", launch.resume)

    def test_the_session_env_wins_over_the_config_env(self):
        config = {**CONFIG, "env": {"K": "v", "PYTHONPATH": "/config"}}
        access = Access(env={"PYTHONPATH": "/root", "S": "s"})
        self.env({"PYTHONPATH": "/parent"})
        for launch, defaults in (
            (self.launch(config=config, access=access), CEILING),
            (
                ClaudeAdapter(config)
                .tui()
                .launch(
                    "hi",
                    SessionRequest(Spec(workdir=self.work, sid=SID, access=access), resumed=False),
                    HOOK,
                ),
                {},
            ),
        ):
            self.assertEqual(
                dict(launch.env), {**defaults, "K": "v", "PYTHONPATH": "/root", "S": "s"}
            )


class AdapterTest(Base):
    def test_its_parts(self):
        adapter = ClaudeAdapter(CONFIG)
        self.assertEqual(
            ClaudeAdapter.keys, frozenset({"flags", "allow", "tiers", "efforts", "env"})
        )
        self.assertEqual((ClaudeAdapter.runtime_note, ClaudeCommon.NOTE), (NOTE, NOTE))
        self.assertIsInstance(adapter.common, UnconfiguredCommon)
        for mode, kind in ((adapter.headless, ClaudeHeadless), (adapter.tui, ClaudeTui)):
            with self.subTest(kind=kind):
                self.assertIs(type(mode()), kind)
                self.assertIsNot(mode(), mode())
                self.assertIs(mode().common, adapter.common)

    def test_the_usage_probe_and_its_reading(self):
        self.assertEqual(ClaudeCommon.PROBE, PROBE)
        self.assertEqual(
            ClaudeAdapter(CONFIG).usage_probe(),
            Launch(PROBE, env={**CEILING, "K": "v"}, unset=UNSET),
        )
        self.assertEqual(ClaudeAdapter({}).usage(event(five=0.25)), Usage(Window(25.0, 1.0), None))
        self.assertIsNone(ClaudeAdapter({}).usage("garbage"))

    def test_session_status(self):
        self.assertEqual(
            ClaudeAdapter({}).session_status(SID, self.work),
            SessionStatus("no", None, f"cd {shlex.quote(self.work)} && claude --resume {SID}"),
        )
        self.assertEqual(
            ClaudeAdapter({}).session_status("x", self.work),
            SessionStatus("no", None, ""),
        )


class WritableDirsTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd, self.added = (os.path.join(self.tmp, name) for name in ("cwd", "added"))
        for path in (self.cwd, self.added):
            os.mkdir(path)
        self.link = os.path.join(self.tmp, "link")
        os.symlink(self.added, self.link)

    def dirs(self, *, resume=False, workdir=None, **access) -> tuple[str, ...]:
        spec = Spec(workdir=workdir or self.work, sid=SID, access=Access(**access))
        return ClaudeAdapter(CONFIG).writable_dirs(SessionRequest(spec, resumed=resume))

    def test_the_workdir_without_a_cwd_then_the_added_dirs(self):
        self.assertEqual(self.dirs(), (self.work,))
        self.assertEqual(self.dirs(dirs=(self.added, self.cwd)), (self.work, self.added, self.cwd))

    def test_the_cwd_first_when_set(self):
        self.assertEqual(self.dirs(cwd=self.cwd), (self.cwd,))
        self.assertEqual(self.dirs(cwd=self.cwd, dirs=(self.added,)), (self.cwd, self.added))

    def test_real_paths_each_once_in_order(self):
        work_link = os.path.join(self.tmp, "work-link")
        os.symlink(self.work, work_link)
        self.assertEqual(self.dirs(workdir=work_link), (self.work,))
        self.assertEqual(
            self.dirs(cwd=self.link, dirs=(self.added, self.work, work_link, self.cwd + "/")),
            (self.added, self.work, self.cwd),
        )

    def test_a_resume_the_same(self):
        for access, dirs in (
            ({}, (self.work,)),
            ({"cwd": self.cwd}, (self.cwd,)),
            ({"dirs": (self.link, self.work)}, (self.work, self.added)),
        ):
            with self.subTest(access=access):
                self.assertEqual(self.dirs(resume=True, **access), dirs)

    def test_read_dirs_never(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(
                    self.dirs(resume=resume, dirs=(self.added,), reads=(self.cwd, self.link)),
                    (self.work, self.added),
                )

    def test_access_and_flag_dirs_are_combined_for_every_session(self):
        os.mkdir(os.path.join(self.cwd, "sub"))
        flags = (
            "--verbose",
            "--add-dir",
            "sub",
            "--add-dir=../added",
            "--add-dir",
            self.link,
            "--",
            "--add-dir",
            "/x",
        )
        for resume in (False, True):
            for cwd, given, dirs in (
                ("", (), (self.work, self.tmp)),
                (self.cwd, (), (self.cwd, self.tmp)),
                (self.cwd, flags, (self.cwd, self.tmp, os.path.join(self.cwd, "sub"), self.added)),
            ):
                with self.subTest(resume=resume, cwd=cwd, flags=given):
                    spec = Spec(
                        workdir=self.work,
                        sid=SID,
                        access=Access(cwd=cwd, dirs=(self.tmp,), reads=(self.tmp,)),
                        flags=given,
                    )
                    self.assertEqual(
                        ClaudeAdapter(CONFIG).writable_dirs(SessionRequest(spec, resumed=resume)),
                        dirs,
                    )
