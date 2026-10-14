"""HarnessDelegate plans before writing and persists its identity for durable views."""

import os
import stat
import subprocess
import sys
import time
from dataclasses import replace

import agent_pm.delegate
from agent_pm.delegate import Begun, ConfigError, Finished, Limit, Outcome, Spec
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import Exited, LimitHit, Reply
from agent_pm.utils import Record, package_dirs

from delegate.tests.case import REPO, child_env
from delegate.tests.fakes.harness_session import (
    FailingSession,
    HarnessSession,
    PersistentSession,
    RecordingHost,
)
from delegate.tests.harness import SID, HarnessCase
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE


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

    def test_protect_reaches_session_spec_with_delegate_package_dirs(self):
        custom = os.path.join(self.home, "protected")
        spec = Spec("input", self.work, cwd=self.work, runtime="fake", protect=(custom,))
        self.make(spec=spec).run()
        session = HarnessSession.started[0]["spec"]
        self.assertEqual(self.metadata()["spec"]["protect"], [custom])
        self.assertIn(custom, session.protect)
        for path in package_dirs(agent_pm.delegate):
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
        self.settings(language="French")
        view = HarnessDelegate.attach(self.work)
        self.settings(language="German", runtime="changed-runtime", default="changed")
        view.resume()
        view.close()
        view.run()
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual((launched.sid, launched.runtime), (SID, "fake"))
        self.assertIn("French", launched.prompt)
        self.assertNotIn("German", launched.prompt)
        self.assertIn(custom, launched.protect)
        for path in package_dirs(agent_pm.delegate):
            self.assertIn(path, launched.protect)
        self.assertEqual(self.metadata()["spec"]["protect"], [custom])


class Completion(HarnessCase):
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
