"""Claude common behavior."""

import json
import os
import shlex
import unittest
from functools import partial
from unittest import mock

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.engine.types import (
    Access,
    SessionStatus,
    Usage,
    Window,
)

from session.tests.case import SPAWNS, Case
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.adapters.claude.support import (
    CEILING,
    CONFIG,
    HOOK,
    SID,
    UNSET,
    Base,
    ClaudeCommon,
    Spec,
    event,
    info_line,
    tui_launch,
    unified,
    usage,
)


class ModelEffortTest(Base):
    def test_none_omits_the_flag_and_a_value_is_the_tables(self):
        common = ClaudeCommon(CONFIG)
        self.assertEqual((common.model(None), common.effort(None)), (None, None))
        self.assertEqual(
            (common.model(1), common.model(4), common.effort("max")), ("fable", "haiku", "mx")
        )

    def test_a_missing_entry_names_the_table(self):
        for config in (CONFIG, {}):
            with self.subTest(config=config):
                common = ClaudeCommon(config)
                self.refused(
                    lambda *, common=common: common.model(2),
                    "no model for tier 2 in [adapters.claude].tiers",
                )
                self.refused(
                    lambda *, common=common: common.effort("medium"),
                    "no effort for 'medium' in [adapters.claude].efforts",
                )
        self.refused(lambda: self.launch(tier=3), "no model for tier 3 in [adapters.claude].tiers")
        self.refused(
            lambda: self.launch(effort="high"), "no effort for 'high' in [adapters.claude].efforts"
        )


class ReadRulesTest(Base):
    def modes(self, *, resume=False, config=None, **access):
        spec = Spec(workdir=self.work, sid=SID, access=Access(**access))
        adapter = ClaudeAdapter({} if config is None else config)
        return (
            lambda: adapter.headless().launch("hi", spec, resume=resume),
            lambda: adapter.tui().launch(
                "hi", spec, HOOK, source_roots=PACKAGE_ROOTS, resume=resume
            ),
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

        self.driver_dependencies = {
            "host": mock.Mock(tmux_path=mock.Mock(return_value="/protected/tmux"))
        }
        self.configure_session({"claude": {"implementation": "adapters.claude:ClaudeAdapter"}})
        for read in bad:
            for mode in ("headless", "tui"):
                with self.subTest(read=read, mode=mode):
                    launched = [self.enterContext(mock.patch(target)) for target in SPAWNS]
                    spec = Spec(workdir=self.work, sid=SID, access=Access(reads=("/ok", read)))
                    self.refused(
                        partial(self.driver.start, "claude", mode, "hi", spec),
                        f"claude: read dir {read!r:.80} must be a printable absolute path without *?[]{{}}()\\,",
                    )
                    for spawn in launched:
                        spawn.assert_not_called()
                    self.assertFalse(os.path.exists(os.path.join(self.work, "run.jsonl")))

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
                    Spec(workdir=self.work, sid=SID, access=access),
                    HOOK,
                    source_roots=PACKAGE_ROOTS,
                    resume=False,
                ),
                {},
            ),
        ):
            self.assertEqual(
                dict(launch.env), {**defaults, "K": "v", "PYTHONPATH": "/root", "S": "s"}
            )


class ParseUsageTest(Case):
    def test_router_gate_cases(self):
        rows = (
            (['{"type":"system"}', "garbage"], False),
            ([event(five=0.85)], True),
            ([event(five=0.89)], True),
            ([event(five=0.9)], False),
            ([event(status="rejected", five=0.1)], False),
            ([event(five=0.1, seven_day=1.0)], False),
            ([event(five=0.89, seven_day=0.99, seven_day_opus=0.5)], True),
            ([event(five=0.1, seven_day=0.2, seven_day_opus=1.0)], False),
            ([event(status="rejected", five=0.1, kind="seven_day_opus", seven_day=0.2)], False),
            ([event(status="allowed_warning", five=0.5)], True),
            ([event(five=0.1), event(five=0.9)], False),
            ([event(five=0.9), "{}", event(five=0.1)], True),
        )
        for row, (lines, ok) in enumerate(rows):
            with self.subTest(row=row):
                found = usage(*lines)
                self.assertEqual(
                    found is not None
                    and found.five_hour is not None
                    and found.five_hour.used_percent < 90
                    and (found.weekly is None or found.weekly.used_percent < 100),
                    ok,
                )

    def test_each_window(self):
        rows = (
            (event(five=0.25), Usage(Window(25.0, 1.0), None)),
            (event(five=0.25, seven_day=0.5), Usage(Window(25.0, 1.0), Window(50.0, 2.0))),
            (event(five=1, seven_day=1), Usage(Window(100.0, 1.0), Window(100.0, 2.0))),
            (event(five=1.5, seven_day_opus=2), Usage(Window(150.0, 1.0), Window(200.0, 2.0))),
            (
                event(five=0.25, seven_day="1", seven_day_sonnet=True),
                Usage(Window(25.0, 1.0), None),
            ),
            (
                event(five=0.25, seven_day=None, seven_day_opus=1e999),
                Usage(Window(25.0, 1.0), None),
            ),
            (event(five=None, seven_day=0.5), Usage(None, Window(50.0, 2.0))),
            (event(five="0.5"), Usage(None, None)),
            (event(five=True), Usage(None, None)),
            (event(five=1e999), Usage(None, None)),
            (event(five=10**400), Usage(None, None)),
            (unified(five_hour=0.5, seven_day=[1]), Usage(None, None)),
            (
                unified(
                    five_hour={"utilization": 0.5},
                    weekly={"utilization": 0.9},
                    one_day={"utilization": 0.9},
                ),
                Usage(Window(50.0, None), None),
            ),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line), want)

    def test_weekly_is_the_most_used_seven_day_window(self):
        rows = (
            (
                event(five=0.1, seven_day=0.3, seven_day_opus=0.8, seven_day_sonnet=0.5),
                Window(80.0, 2.0),
            ),
            (
                unified(
                    seven_day={"utilization": 0.5, "resetsAt": 3},
                    seven_day_opus={"utilization": 0.5, "resetsAt": 4},
                ),
                Window(50.0, 3.0),
            ),
            (
                unified(
                    seven_day_opus={"utilization": 0.5, "resetsAt": 4},
                    seven_day={"utilization": 0.5, "resetsAt": 3},
                ),
                Window(50.0, 4.0),
            ),
            (
                unified(seven_day={"utilization": "0.9"}, seven_day_opus={"utilization": 0.4}),
                Window(40.0, None),
            ),
            (
                unified(
                    seven_day_x={"utilization": 0.7, "resetsAt": 5}, seven_day={"utilization": 0.2}
                ),
                Window(70.0, 5.0),
            ),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line).weekly, want)

    def test_each_window_resets_at_its_own_time(self):
        rows = (
            ({"resetsAt": 1700000000.5}, Window(25.0, 1700000000.5)),
            ({}, Window(25.0, None)),
            ({"resetsAt": None}, Window(25.0, None)),
            ({"resetsAt": True}, Window(25.0, None)),
            ({"resetsAt": "1"}, Window(25.0, None)),
            ({"resetsAt": 10**400}, Window(25.0, None)),
        )
        for row, (resets, want) in enumerate(rows):
            with self.subTest(row=row):
                line = info_line(
                    {
                        "status": "allowed",
                        "resetsAt": 9,
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.25, **resets},
                            "seven_day": {"utilization": 0.5, **resets},
                        },
                    }
                )
                self.assertEqual(usage(line), Usage(want, Window(50.0, want.resets_at)))

    def test_a_rejected_status_fills_its_window(self):
        rows = (
            (event(status="rejected", five=0.25, resets=9), Usage(Window(100.0, 1.0), None)),
            (event(status="rejected", five=1.5, resets=9), Usage(Window(150.0, 1.0), None)),
            (
                event(status="rejected", five=0.25, kind=None, resets=9),
                Usage(Window(100.0, 1.0), None),
            ),
            (
                event(status="rejected", five=0.25, kind=5, resets=9),
                Usage(Window(100.0, 1.0), None),
            ),
            (
                event(status="rejected", five=0.25, kind="five_hour_x", seven_day=0.5, resets=9),
                Usage(Window(100.0, 1.0), Window(50.0, 2.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day", resets=9),
                Usage(Window(25.0, 1.0), Window(100.0, 9.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day_opus", seven_day=0.5, resets=9),
                Usage(Window(25.0, 1.0), Window(100.0, 2.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day", seven_day=1.5, resets=9),
                Usage(Window(25.0, 1.0), Window(150.0, 2.0)),
            ),
            (event(status="rejected", five="x", resets=9), Usage(Window(100.0, 9.0), None)),
            (event(status="rejected", five=0.25, resets=None), Usage(Window(100.0, 1.0), None)),
            (event(status="rejected", five=None, resets=None), Usage(Window(100.0, None), None)),
            (event(status="rejected", five=None, resets=True), Usage(Window(100.0, None), None)),
            (
                info_line({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 9}),
                Usage(Window(100.0, 9.0), None),
            ),
            (
                info_line(
                    {
                        "status": "rejected",
                        "rateLimitType": "seven_day",
                        "resetsAt": 9,
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.25},
                            "seven_day": {"utilization": 0.5},
                        },
                    }
                ),
                Usage(Window(25.0, None), Window(100.0, 9.0)),
            ),
            (
                info_line(
                    {
                        "status": "rejected",
                        "resetsAt": 9,
                        "unifiedWindows": {"five_hour": {"utilization": 0.25}},
                    }
                ),
                Usage(Window(100.0, 9.0), None),
            ),
            (event(status="allowed_warning", five=0.25, resets=9), Usage(Window(25.0, 1.0), None)),
            (event(status="Rejected", five=0.25, resets=9), Usage(Window(25.0, 1.0), None)),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line), want)

    def test_an_event_without_windows_is_a_reading(self):
        for line in (
            info_line({}),
            info_line({"status": "allowed", "resetsAt": 9}),
            info_line({"unifiedWindows": {}}),
            info_line({"unifiedWindows": [1]}),
            info_line({"unifiedWindows": None}),
            info_line({"unifiedWindows": {"five_hour": 0.5}}),
        ):
            with self.subTest(line=line):
                self.assertEqual(usage(line), Usage(None, None))

    def test_no_event_or_a_bad_info_is_none(self):
        rows = (
            (),
            ("garbage",),
            ('{"type":"system"}',),
            ("[" * 100000,),
            ('[{"type": "rate_limit_event"}]',),
            (json.dumps({"type": "rate_limit_event"}),),
            (info_line(None),),
            (info_line([1]),),
            (info_line("x"),),
            (event(five=0.1), info_line(None)),
        )
        for lines in rows:
            with self.subTest(lines=[x[:40] for x in lines]):
                self.assertIsNone(usage(*lines))


class TranscriptTest(Base):
    def test_projects_dir_named_by_the_mangled_cwd(self):
        self.assertEqual(
            ClaudeCommon({}).transcript("/agent-pm-absent/a.b_c d-é", SID),
            os.path.join(self.config_dir, "projects", "-agent-pm-absent-a-b-c-d--", f"{SID}.jsonl"),
        )

    def test_the_cwds_realpath(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        common = ClaudeCommon({})
        self.assertEqual(common.transcript(link, SID), common.transcript(self.work, SID))
        self.assertNotIn("link", common.transcript(link, SID))

    def test_the_config_dir_from_the_config_env_then_the_process_env_then_home(self):
        given = {"env": {"CLAUDE_CONFIG_DIR": "/given"}}
        self.assertEqual(
            ClaudeCommon(given).transcript("/w", SID), f"/given/projects/-w/{SID}.jsonl"
        )
        self.assertEqual(ClaudeCommon(given).config_dir(), "/given")
        self.assertEqual(ClaudeCommon({}).config_dir(), self.config_dir)
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(
            ClaudeCommon({}).transcript("/w", SID),
            os.path.join(self.home, ".claude", "projects", "-w", f"{SID}.jsonl"),
        )

    def test_the_launch_transcript_is_in_its_cwd(self):
        cwd = os.path.join(self.tmp, "cwd")
        common = ClaudeCommon({})
        self.assertEqual(self.launch().transcript, common.transcript(self.work, SID))
        self.assertEqual(
            self.launch(access=Access(cwd=cwd)).transcript, common.transcript(cwd, SID)
        )


class ResumeTest(Base):
    def test_the_command_quoted(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w d", SID), f"cd '/w d' && claude --resume {SID}"
        )

    def test_each_granted_dir_is_added(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w", SID, ("/o x", "/p")),
            f"cd /w && claude --resume {SID} --add-dir '/o x' --add-dir /p",
        )

    def test_the_launch_resume_adds_the_granted_dirs_never_the_workdir(self):
        cwd = os.path.join(self.tmp, "cwd")
        q = shlex.quote
        self.assertEqual(self.launch().resume, f"cd {q(self.work)} && claude --resume {SID}")
        for launch in (self.launch, partial(tui_launch, self.work)):
            with self.subTest(launch=launch):
                self.assertEqual(
                    launch(access=Access(cwd=cwd)).resume, f"cd {q(cwd)} && claude --resume {SID}"
                )
                self.assertEqual(
                    launch(access=Access(cwd=cwd, dirs=(self.work, "/d"))).resume,
                    f"cd {q(cwd)} && claude --resume {SID} --add-dir {q(self.work)} --add-dir /d",
                )

    def test_the_launch_resume_assigns_the_session_env_quoted(self):
        env = {"PYTHONPATH": "/a b;c", "S": "s"}
        resume = self.launch(access=Access(env=env)).resume
        self.assertEqual(
            resume,
            f"cd {shlex.quote(self.work)} && PYTHONPATH='/a b;c' S=s claude --resume {SID}",
        )
        self.assertEqual(shlex.split(resume)[3:6], ["PYTHONPATH=/a b;c", "S=s", "claude"])


class SessionStatusTest(Base):
    def setUp(self):
        super().setUp()
        self.common = ClaudeCommon({})
        self.path = self.common.transcript(self.work, SID)
        self.resume = f"cd {shlex.quote(self.work)} && claude --resume {SID}"

    def write(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.utime(path, (mtime, mtime))

    def status(self, sid=SID) -> SessionStatus:
        return self.common.session_status(sid, self.work)

    def test_a_transcript_is_resumable_with_its_mtime(self):
        self.write(self.path, 1000)
        self.assertEqual(self.status(), SessionStatus("yes", 1000.0, self.resume))

    def test_the_newest_file_under_the_session_dir_wins(self):
        self.write(self.path, 1000)
        sub = self.path[: -len(".jsonl")]
        self.write(os.path.join(sub, "subagents", "old.jsonl"), 500)
        self.assertEqual(self.status().last_active, 1000.0)
        self.write(os.path.join(sub, "subagents", "workflows", "r", "journal.jsonl"), 2000)
        self.write(os.path.join(sub, "subagents", "mid.jsonl"), 1500)
        self.assertEqual(self.status(), SessionStatus("yes", 2000.0, self.resume))

    def test_a_missing_transcript_is_not_resumable(self):
        self.assertEqual(self.status(), SessionStatus("no", None, self.resume))
        os.makedirs(os.path.dirname(os.path.dirname(self.path)))
        with open(os.path.dirname(self.path), "w", encoding="utf-8"):
            pass
        self.assertEqual(self.status().resumable, "no")

    @unittest.skipIf(os.geteuid() == 0, "root reads a 000 dir")
    def test_an_unreadable_project_dir_is_unknown(self):
        self.write(self.path, 1000)
        project = os.path.dirname(self.path)
        os.chmod(project, 0)
        self.addCleanup(os.chmod, project, 0o700)
        self.assertEqual(self.status(), SessionStatus("unknown", None, self.resume))

    def test_a_transcript_that_is_no_regular_file_is_unknown(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.assertEqual(self.status().resumable, "unknown")
        os.remove(self.path)
        target = os.path.join(self.tmp, "target.jsonl")
        self.write(target, 1000)
        os.symlink(target, self.path)
        self.assertEqual(self.status().resumable, "unknown")

    def test_a_sid_that_is_no_uuid_is_not_resumable(self):
        for sid in ("abc", SID.upper(), "../" + SID):
            with self.subTest(sid=sid):
                self.assertEqual(self.status(sid), SessionStatus("no", None, ""))
