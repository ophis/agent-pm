"""HarnessDelegate plans before writing and persists its identity for durable views."""

import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace

from agent_pm.delegate import Begun, ConfigError, Finished, Limit, Outcome, Progress, Spec
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Exited, LimitHit, Liveness, Reply, Usage, Window
from agent_pm.utils import Record

from delegate.tests.case import REPO, child_env
from delegate.tests.fakes.harness_session import (
    FailingSession,
    HarnessSession,
    PersistentSession,
    RecordingHost,
)
from delegate.tests.fakes.session import SessionClass
from delegate.tests.harness import SID, HarnessCase
from delegate.tests.replies import block, mark
from delegate.tests.unit.runs import DONE

USAGE_QUERY = """import io, json, unittest
from unittest import mock
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import Usage, Window
from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost
from delegate.tests.fakes.usage import UsageAdapter
case = unittest.TestCase()
sentinel = Usage(Window(12.5, 1767225600.0), Window(37.0, None))
probes = []
for expected in (sentinel, None):
    UsageAdapter.usage_result = expected
    process = mock.Mock(stdout=io.StringIO("quota"))
    process.communicate.return_value = ("quota", "")
    with mock.patch("subprocess.Popen", return_value=process) as probe:
        case.assertIs(HarnessDelegate.usage("dummy-tester"), expected)
    case.assertEqual(probe.call_count, 1)
    probes.append(probe.call_count)
case.assertEqual(UsageAdapter.queried, ["role_usage", "role_usage"])
case.assertEqual((UsageAdapter.starts, HarnessSession.started, RecordingHost.launches), ([], [], []))
print(json.dumps({"queried": UsageAdapter.queried, "probes": probes,
                  "starts": UsageAdapter.starts, "session_starts": HarnessSession.started,
                  "launches": RecordingHost.launches}))
"""


class ConfiguredSession(HarnessSession):
    """Its Hosts are the configured [hosts], as HostedSession's are."""

    @classmethod
    def host(cls, name=None):
        if name is not None and name not in cls.configured["hosts"].tables:
            raise SessionConfigError(f"unknown host {name}")
        return super().host(name)


def _close_child(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


class Planning(HarnessCase):
    def test_constructor_persists_resolved_metadata_0600_without_starting(self):
        callback = ["writeback", "--issue", "ticket-id"]
        self.make(host="agent", harness_host="harness", display="opaque-display", callback=callback)
        path = os.path.join(self.work, "delegate.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        data = self.metadata()
        self.assertEqual(
            (
                data["role"],
                data["host"],
                data["harness_host"],
                data["display"],
                data["callback"],
                data["attempt"],
            ),
            ("dummy-tester", "agent", "harness", "opaque-display", callback, 1),
        )
        self.assertEqual(data["spec"]["sid"], SID)
        self.assertEqual(data["spec"]["input"], "Hello.\nLiteral input.")
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_invalid_role_task_and_mode_write_nothing(self):
        original = Spec("input", self.work, cwd=self.work)
        for role, spec in (
            ("absent-role", original),
            ("dummy-tester", replace(original, task="absent-task")),
            ("dummy-tester", replace(original, mode="absent-mode")),
        ):
            with self.subTest(role=role, spec=spec), self.assertRaises(ConfigError):
                self.make(role=role, spec=spec)
            self.assertFalse(os.path.exists(self.work))
            self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_invalid_replacement_preserves_existing_files_and_metadata(self):
        self.make()
        before = self.metadata()
        marker = os.path.join(self.work, "draft.md")
        with open(marker, "w", encoding="utf-8") as file:
            file.write("Draft")
        files = set(os.listdir(self.work))
        with self.assertRaises(ConfigError):
            self.make(role="absent-role")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(set(os.listdir(self.work)), files)
        with open(marker, encoding="utf-8") as file:
            self.assertEqual(file.read(), "Draft")

    def test_live_owner_refuses_replacement_without_writing(self):
        delegate = self.make(host="agent", harness_host="harness")
        record = delegate.detach()
        before = self.metadata()
        with self.assertRaises(ConfigError):
            self.make()
        self.assertEqual(self.metadata(), before)
        self.assertTrue(HarnessDelegate.attach(record).alive())

    def test_ended_run_replacement_counts_attempt_and_stale_detach_is_refused(self):
        first = self.make()
        self.make()
        self.assertEqual(self.metadata()["attempt"], 2)
        with self.assertRaises(ConfigError):
            first.detach()
        self.assertEqual(RecordingHost.launches, [])

    def test_a_harness_host_named_default_is_refused_naming_the_configured_hosts(self):
        self.settings(harness_host="default")
        with self.assertRaises(ConfigError) as caught:
            self.make()
        self.assertEqual(
            str(caught.exception),
            "[delegates] harness_host: want one of agent, changed, harness, tmux",
        )
        self.assertFalse(os.path.lexists(self.work))

    def test_replacing_a_run_whose_harness_host_left_the_config_is_refused(self):
        ConfiguredSession.reset()
        self.make(harness_host="harness", session_cls=ConfiguredSession).detach()
        with open(os.path.join(self.work, "delegate.json"), "rb") as file:
            before = file.read()
        self.settings(hosts=("agent", "changed"))
        with self.assertRaises(ConfigError) as caught:
            self.make(session_cls=ConfiguredSession)
        self.assertEqual(str(caught.exception), "unknown host harness")
        with open(os.path.join(self.work, "delegate.json"), "rb") as file:
            self.assertEqual(file.read(), before)

    def test_protect_reaches_session_spec_with_delegate_package_dirs(self):
        team = self.team_dir()
        self.settings(team_dirs=(team,))
        custom = os.path.join(self.home, "protected")
        spec = Spec("input", self.work, cwd=self.work, runtime="fake", protect=(custom,))
        self.make(spec=spec).run()
        session = HarnessSession.started[0]["spec"]
        self.assertEqual(self.metadata()["spec"]["protect"], [custom])
        self.assertIn(custom, session.protect)
        self.assertIn(os.path.realpath(team), session.protect)
        for name in ("delegate", "session", "utils"):
            path = os.path.realpath(os.path.join(REPO, name, "src", "agent_pm", name))
            self.assertIn(path, session.protect)

    def test_constructor_keeps_its_config_snapshot_when_local_file_changes(self):
        delegate = self.make(spec=Spec("input", self.work, task="echo", cwd=self.work, sid=SID))
        self.settings(language="French", runtime="another-runtime")
        delegate.run()
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual(launched.runtime, "fake")
        self.assertIn("English", launched.prompt)
        self.assertNotIn("French", launched.prompt)


class Views(HarnessCase):
    def test_attach_keeps_identity_after_default_runtime_and_rules_change(self):
        delegate = self.make(host="agent", harness_host="harness", display="original")
        record = delegate.detach(
            outer={"kind": "outer", "ctl": ["owner"], "resume": ["owner", "resume", "id"]}
        )
        before = self.metadata()
        self.settings(language="French", runtime="changed-runtime", default="changed")
        view = HarnessDelegate.attach(record)
        self.assertTrue(view.alive())
        self.assertEqual(view.resume(), record)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(HarnessSession.started, [])
        self.assertEqual(len(RecordingHost.launches), 1)

    def test_attach_refuses_missing_malformed_and_foreign_metadata(self):
        os.mkdir(self.work)
        for value in (None, "not json", "{}", '{"worker": true}'):
            with self.subTest(value=value):
                if value is not None:
                    with open(
                        os.path.join(self.work, "delegate.json"), "w", encoding="utf-8"
                    ) as file:
                        file.write(value)
                with self.assertRaises(ConfigError):
                    HarnessDelegate.attach(self.work)
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_attach_refuses_a_stored_host_removed_from_config(self):
        for removed in ("changed", "harness"):
            with self.subTest(removed=removed):
                ConfiguredSession.reset()
                self.settings()
                self.work = os.path.join(self.home, removed)
                self.make(
                    host="changed", harness_host="harness", session_cls=ConfiguredSession
                ).detach()
                self.settings(
                    hosts=tuple(h for h in ("agent", "harness", "changed") if h != removed)
                )
                with self.assertRaises(ConfigError) as caught:
                    HarnessDelegate.attach(self.work, session_cls=ConfiguredSession)
                self.assertEqual(str(caught.exception), f"unknown host {removed}")

    def test_resume_refuses_a_cwd_no_longer_trusted_and_launches_nothing(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        self.settings(trusted_dirs=(project,))
        spec = Spec("input", self.work, task="echo", sid=SID, cwd=project, runtime="fake")
        record = self.make(spec=spec).detach()
        self.assertTrue(self.metadata()["plan"]["session"]["access"]["project"])
        self.stop(record)
        self.settings()
        view = HarnessDelegate.attach(self.work)
        launches, starts = len(RecordingHost.launches), len(HarnessSession.started)
        with self.assertRaises(ConfigError) as caught:
            view.resume()
        self.assertEqual(str(caught.exception), f"cwd {project} is no longer in trusted_dirs")
        self.assertEqual(
            (len(RecordingHost.launches), len(HarnessSession.started)), (launches, starts)
        )

    def test_a_completed_foreground_tui_left_open_is_not_alive(self):
        spec = Spec(
            "input", self.work, task="echo", sid=SID, cwd=self.work, runtime="fake", mode="tui"
        )
        delegate = self.make(spec=spec)
        self.assertEqual(delegate.run().outcome.status, "done")
        agent = HarnessSession.attach(f"dummy-tester-{SID[:8]}")
        self.assertEqual(agent.state(), Liveness.LIVE)
        self.assertEqual(
            (delegate.alive(), HarnessDelegate.attach(self.work).alive()), (False, False)
        )

    def test_a_completed_object_refuses_a_second_run_keeping_its_result(self):
        delegate = self.make()
        delegate.run()
        path = os.path.join(self.work, "delegate.json")
        with open(path, "rb") as file:
            before = file.read()
        starts = len(HarnessSession.started)
        with self.assertRaises(ConfigError) as caught:
            delegate.run()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate run already completed")
        with open(path, "rb") as file:
            self.assertEqual((file.read(), len(HarnessSession.started)), (before, starts))

    def test_a_completed_object_refuses_to_detach_launching_nothing(self):
        delegate = self.make()
        delegate.run()
        launched = list(RecordingHost.launches)
        with self.assertRaises(ConfigError) as caught:
            delegate.detach()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate run already completed")
        self.assertEqual(RecordingHost.launches, launched)

    def test_completed_session_info_uses_public_history_without_starting(self):
        delegate = self.make()
        delegate.run()
        delegate.close()
        count = len(HarnessSession.started)
        view = HarnessDelegate.attach(self.work)
        self.assertEqual(view.session_info(), HarnessSession.history)
        self.assertEqual(len(HarnessSession.started), count)
        self.assertEqual(HarnessSession.inspected[-1].sid, SID)

    def test_valid_outcome_resume_does_nothing_for_each_terminal_status(self):
        for status in ("done", "needs_input", "failed"):
            with self.subTest(status=status):
                self.work = os.path.join(self.home, status)
                data: dict = dict(DONE, status=status)
                if status == "needs_input":
                    data["questions"] = ["Continue?"]
                HarnessSession.script = (Reply(block(data)), Exited(0))
                delegate = self.make()
                result = delegate.run()
                self.assertEqual(result.outcome, Outcome(**data))
                view = HarnessDelegate.attach(self.work)
                starts = len(HarnessSession.started)
                launches = len(RecordingHost.launches)
                first = view.resume()
                self.assertEqual(view.resume(), first)
                self.assertEqual(
                    (len(HarnessSession.started), len(RecordingHost.launches)), (starts, launches)
                )

    def test_latest_interrupted_attempt_does_not_reuse_an_earlier_outcome(self):
        first = self.make()
        first.run()
        first.close()
        self.make()
        view = HarnessDelegate.attach(self.work)
        self.assertIsNone(view.outcome())
        record = view.resume()
        self.assertTrue(view.alive())
        self.assertEqual(view.resume(), record)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_resume_uses_attach_config_snapshot_and_preserves_protect(self):
        team = self.team_dir()
        self.settings(team_dirs=(team,))
        custom = os.path.join(self.home, "protected")
        self.make(
            spec=Spec(
                "input",
                self.work,
                task="echo",
                cwd=self.work,
                runtime="fake",
                sid=SID,
                protect=(custom,),
            )
        )
        self.settings(language="French", team_dirs=(team,))
        view = HarnessDelegate.attach(self.work)
        self.settings(
            language="German", runtime="changed-runtime", default="changed", team_dirs=(team,)
        )
        view.resume()
        view.close()
        view.run()
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual((launched.sid, launched.runtime), (SID, "fake"))
        self.assertIn("French", launched.prompt)
        self.assertNotIn("German", launched.prompt)
        self.assertIn(custom, launched.protect)
        self.assertIn(os.path.realpath(team), launched.protect)
        for name in ("delegate", "session", "utils"):
            path = os.path.realpath(os.path.join(REPO, name, "src", "agent_pm", name))
            self.assertIn(path, launched.protect)
        self.assertEqual(self.metadata()["spec"]["protect"], [custom])


class UsageQueries(HarnessCase):
    def test_role_runtime_usage_preserves_sentinel_identity_and_none_without_starting(self):
        self.settings(runtime="role_usage")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.adapters.role_usage]\n"
                'implementation = "delegate.tests.fakes.usage:UsageAdapter"\n'
                'label = "role_usage"\n'
                f"cwd = {json.dumps(self.home)}\n"
            )
        result = subprocess.run(
            [sys.executable, "-P", "-S", "-c", USAGE_QUERY],
            cwd=self.home,
            env=child_env(None, REPO),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "queried": ["role_usage", "role_usage"],
                "probes": [1, 1],
                "starts": [],
                "session_starts": [],
                "launches": [],
            },
        )

    def test_usage_configures_and_asks_only_the_given_session_class(self):
        sentinel = Usage(Window(12.5, 1767225600.0), Window(37.0, None))
        asked = []

        class Probe(SessionClass):
            @classmethod
            def usage(cls, runtime):
                asked.append(runtime)
                return sentinel

        self.assertIs(HarnessDelegate.usage("dummy-tester", session_cls=Probe), sentinel)
        self.assertEqual(asked, ["fake"])
        self.assertEqual(
            sorted(Probe.configured["hosts"].tables), ["agent", "changed", "harness", "tmux"]
        )

    def test_session_config_errors_are_the_delegates(self):
        class Refusing(SessionClass):
            @classmethod
            def usage(cls, runtime):
                raise SessionConfigError(f"[adapters.{runtime}]: no usage probe")

        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.usage("dummy-tester", session_cls=Refusing)
        self.assertEqual(str(caught.exception), "[adapters.fake]: no usage probe")
        self.settings(default="nope")
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.usage("dummy-tester", session_cls=SessionClass)
        self.assertEqual(
            str(caught.exception), "[hosts] default: want one of agent, changed, harness, tmux"
        )


class Completion(HarnessCase):
    def test_foreground_headless_owns_live_session_and_publishes_result_before_finished(self):
        HarnessSession.script = (Reply(mark("start", "working")), Reply(block(DONE)), Exited(0))
        delegate = self.make()
        seen = []

        def observe(event):
            seen.append(event)
            if isinstance(event, (Begun, Progress)):
                self.assertTrue(delegate.alive())
                self.assertEqual(delegate.session_info(), HarnessSession.history)
                self.assertEqual(HarnessSession.inspected, [])
                self.assertIsNone(delegate.outcome())
            else:
                self.assertFalse(delegate.alive())
                self.assertEqual(delegate.outcome(), event.result.outcome)
                view = HarnessDelegate.attach(self.work)
                self.assertFalse(view.alive())
                self.assertEqual(view.outcome(), event.result.outcome)

        result = delegate.run(observe)
        self.assertEqual([type(event) for event in seen], [Begun, Progress, Finished])
        self.assertEqual(seen[-1], Finished(result))
        self.assertEqual(HarnessSession.started[0]["spec"].sid, SID)

    def test_headless_keeps_display_for_harness_and_passes_none_to_agent(self):
        delegate = self.make(display="opaque", host="agent", harness_host="harness")
        record = delegate.detach()
        self.assertEqual(record.display, "opaque")
        self.assertEqual(RecordingHost.launches[0]["display"], "opaque")
        self.stop(record)
        delegate.run()
        self.assertIsNone(HarnessSession.started[0]["display"])
        self.assertEqual(self.metadata()["display"], "opaque")

    def test_finished_callback_queries_the_completed_result_before_run_returns(self):
        delegate = self.make()
        finished = []

        def observe(event):
            if isinstance(event, Finished):
                finished.append(event)
                view = HarnessDelegate.attach(self.work)
                self.assertFalse(view.alive())
                self.assertEqual(view.outcome(), event.result.outcome)
                self.assertEqual(view.status().outcome, "done")

        result = delegate.run(observe)
        self.assertEqual(finished, [Finished(result)])

    def test_every_returned_result_has_one_last_finished_event(self):
        for script in (
            (Reply(block(DONE)), Exited(0)),
            (Reply("no outcome"), Exited(0)),
            (Exited(7),),
            (LimitHit(1767225600.0), Exited(0)),
        ):
            with self.subTest(script=script):
                self.work = os.path.join(self.home, f"completion-{len(HarnessSession.started)}")
                HarnessSession.script = script
                seen = []
                result = self.make().run(seen.append)
                self.assertIsInstance(seen[0], Begun)
                self.assertEqual(
                    [event for event in seen if isinstance(event, Finished)], [Finished(result)]
                )
                self.assertEqual(seen[-1], Finished(result))
                if isinstance(script[0], LimitHit):
                    self.assertIsNone(result.outcome)
                    self.assertEqual(result.limit, Limit(1767225600.0))
                    self.assertFalse(HarnessSession.live)

    def test_spawn_failure_is_finished_without_begun(self):
        FailingSession.reset()
        delegate = HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, cwd=self.work, runtime="fake"),
            session_cls=FailingSession,
        )
        seen = []
        result = delegate.run(seen.append)
        self.assertEqual(result.returncode, 1)
        self.assertIn("fake spawn refused", result.error)
        self.assertEqual(seen, [Finished(result)])

    def test_on_event_exception_keeps_original_exception_and_stops_session(self):
        failure = RuntimeError("callback failure")
        seen = []

        def callback(event):
            seen.append(event)
            raise failure

        delegate = self.make()
        with self.assertRaises(RuntimeError) as caught:
            delegate.run(callback)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], Begun)
        self.assertFalse(HarnessSession.live)


class ConcurrentResume(HarnessCase):
    def test_two_processes_resume_once_and_return_the_same_record(self):
        delegate = HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, cwd=self.work, runtime="fake", sid=SID),
            session_cls=PersistentSession,
            host="agent",
            harness_host="harness",
            display="opaque",
        )
        code = (
            "import os, sys, time\n"
            "from agent_pm.delegate.engine.harness_delegate import HarnessDelegate\n"
            "open(sys.argv[2] + str(os.getpid()), 'w').close()\n"
            "while not os.path.exists(sys.argv[3]): time.sleep(0.01)\n"
            "print(HarnessDelegate.attach(sys.argv[1]).resume().to_line())\n"
        )
        ready, gate = os.path.join(self.home, "ready-"), os.path.join(self.home, "go")
        children = [
            subprocess.Popen(
                [sys.executable, "-P", "-S", "-c", code, self.work, ready, gate],
                cwd=self.home,
                env=child_env(None, REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            for _ in range(2)
        ]
        for child in children:
            self.addCleanup(_close_child, child)
        deadline = time.monotonic() + 10
        while not all(os.path.exists(ready + str(child.pid)) for child in children):
            self.assertLess(time.monotonic(), deadline, "resume children did not reach barrier")
            time.sleep(0.01)
        with open(gate, "w", encoding="utf-8"):
            pass
        records = []
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr)
            records.append(Record.parse(stdout))
        self.assertEqual(records[0], records[1])
        self.assertEqual((records[0].sid, records[0].display), (SID, "opaque"))
        path = os.path.join(self.agent_pm, "harness-fake", "launches.jsonl")
        with open(path, encoding="utf-8") as file:
            self.assertEqual(len(file.readlines()), 1)
        self.assertTrue(delegate.alive())
