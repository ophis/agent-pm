"""headless events integration test behavior."""

import dataclasses
import json

from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Activity,
    Exited,
    LimitHit,
    Reply,
    SessionBound,
    Stderr,
    TurnEnd,
)

from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.headless_fixtures import (
    DONE,
    NATIVE,
    HeadlessSessions,
)


class HeadlessEventsIntegrationTest(HeadlessSessions):
    def test_driver_replies_native_sid_and_snapshot(self):
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )

        def bound(event):
            if isinstance(event, SessionBound):
                self.assertEqual(
                    self.common.load(str(self.work), self.sid).native_id, event.native_id
                )

        self.assertEqual(
            self.drain(handle, bound),
            [
                SessionBound(NATIVE),
                Reply("one\n\nnext line\n"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three"),
                Exited(0),
            ],
        )
        self.assertEqual(handle.sid, self.sid)
        self.assertEqual(handle.info().native_id, NATIVE)
        self.assertEqual(handle.reply, "three")
        (entry,) = self.logged()
        self.assertEqual(
            (entry["snapshot"]["sid"], entry["snapshot"]["native_id"]), (self.sid, None)
        )
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual(
            [
                (entry["kind"], entry.get("client_id"))
                for entry in self.written()
                if entry["kind"] != "session_metadata"
            ],
            [("session", None), ("client_session", NATIVE)],
        )

    def test_raw_progress_outcome_and_json_reply_are_uninterpreted(self):
        texts = [
            "[agent-pm-progress:start] working\n\n",
            json.dumps({"kind": "outcome", "outcome": DONE}),
            json.dumps({"status": "needs_input", "questions": ["Which greeting?"]}),
            json.dumps({"kind": "outcome", "outcome": {"status": "maybe"}}),
            json.dumps(DONE),
        ]
        self.scene(
            [*(fake_codex.message(text) for text in texts), {"out": {"type": "turn.completed"}}]
        )
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )
        self.assertEqual(
            self.drain(handle),
            [SessionBound(NATIVE), *(Reply(text) for text in texts), TurnEnd(texts[-1]), Exited(0)],
        )
        self.assertEqual(handle.reply, texts[-1])
        self.assertEqual(
            [entry["kind"] for entry in self.written() if entry["kind"] != "session_metadata"],
            ["session", "client_session"],
        )
        self.assertFalse((self.work / "out.md").exists())

    def test_stderr_is_an_event_and_run_record(self):
        self.scene(
            [
                {"err": "Codex diagnostic\nsecond line"},
                fake_codex.message("done"),
                {"out": {"type": "turn.completed"}},
            ]
        )
        found = self.drain(
            self.driver.start(
                "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        self.assertEqual(
            [event for event in found if isinstance(event, Stderr)],
            [Stderr("Codex diagnostic"), Stderr("second line")],
        )
        self.assertEqual(
            [entry["text"] for entry in self.written() if entry["kind"] == "stderr"],
            ["Codex diagnostic", "second line"],
        )
        self.assertEqual(found[-1], Exited(0))

    def test_nonzero_exit_preserves_raw_reply_and_final_turn(self):
        text = json.dumps(DONE)
        self.scene([fake_codex.message(text), {"out": {"type": "turn.completed"}}, {"exit": 7}])
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )
        self.assertEqual(
            self.drain(handle), [SessionBound(NATIVE), Reply(text), TurnEnd(text), Exited(7)]
        )
        self.assertEqual(handle.reply, text)
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)

    def test_nonzero_exit_without_final_turn_is_not_inferred(self):
        self.scene([fake_codex.message("Thinking."), {"exit": 7}])
        self.assertEqual(
            self.drain(
                self.driver.start(
                    "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
                )
            ),
            [SessionBound(NATIVE), Reply("Thinking."), Exited(7)],
        )

    def test_activity_and_usage_limit_pass_through_driver(self):
        error = "You’ve hit your usage limit. Try again later."
        self.scene(
            [
                {"out": {"type": "item.started", "item": {"type": "command_execution"}}},
                {"out": {"type": "error", "message": error}},
                {"out": {"type": "turn.completed"}},
            ]
        )
        self.assertEqual(
            self.drain(
                self.driver.start(
                    "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
                )
            ),
            [
                SessionBound(NATIVE),
                Activity("item.started"),
                Stderr(error),
                LimitHit(None),
                TurnEnd(""),
                Exited(0),
            ],
        )

    def test_caller_sid_and_handle_info_inspect_native_binding(self):
        spec = dataclasses.replace(self.spec, sid=None)
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(spec, sid="caller-owned")
        )
        self.drain(handle)
        info = handle.info()
        self.assertEqual(
            (
                handle.sid,
                info.sid,
                info.native_id,
                info.cwd,
                info.project,
                info.transcript,
                info.resume,
            ),
            (
                "caller-owned",
                "caller-owned",
                NATIVE,
                str(self.cwd),
                True,
                str(fake_codex.session_path(NATIVE)),
                info.resume,
            ),
        )
        self.assertIn(NATIVE, info.resume)
        self.assertEqual(
            records.session_record(str(self.work), handle.sid)["transcript"], info.transcript
        )
        status = self.driver.session_status("codex", handle.sid, str(self.cwd))
        self.assertEqual(status.resumable, "yes")
        self.assertIsNotNone(status.last_active)
        self.assertIn(NATIVE, status.resume_command)
        fake_codex.session_path(NATIVE).unlink()
        self.assertEqual(
            self.driver.session_status("codex", handle.sid, str(self.cwd)).resumable, "no"
        )
        self.record().write_text("bad json")
        self.assertEqual(
            self.driver.session_status("codex", handle.sid, str(self.cwd)).resumable, "unknown"
        )
