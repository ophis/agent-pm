import json

from adapters.base import Activity, ConfigError, LimitHit, Reply, SessionBound, Stderr, TurnEnd
from new.tests.unit.adapters.codex.support import Base, NATIVE


class HeadlessRunTest(Base):
    def run_instance(self):
        run = self.adapter.headless_run()
        run.launch("hello", self.spec, False)
        return run

    def test_native_binding_precedes_event(self):
        run = self.run_instance()
        event, = run.parse({"type": "thread.started", "thread_id": NATIVE})
        self.assertEqual(event, SessionBound(NATIVE))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_bind_failure_never_emits_session_bound(self):
        run = self.run_instance()
        self.record().unlink()
        with self.assertRaises(ConfigError):
            list(run.parse({"type": "thread.started", "thread_id": NATIVE}))

    def test_complete_messages_and_final_result_keep_blank_lines(self):
        run = self.adapter.headless_run()
        texts = ["one\n\nsecond line\n", "[agent-pm-progress:start] x", "three"]
        lines = [json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
                 for text in texts]
        lines.append(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
        self.assertEqual(list(run.events(lines)), [*(Reply(text) for text in texts), TurnEnd(texts[-1])])

    def test_activity_and_invalid_record_diagnostics(self):
        run = self.adapter.headless_run()
        self.assertEqual(list(run.events(["not json", "[]"])), [Stderr("codex: invalid JSON event")])
        self.assertEqual(list(run.parse({"type": "thread.started", "thread_id": "not-a-uuid"})),
                         [Stderr("codex: invalid thread.started ID")])
        self.assertEqual(list(run.parse({"type": "item.started", "item": {"type": "command_execution"}})),
                         [Activity("item.started")])

    def test_source_usage_errors_emit_limit_without_unreliable_reset(self):
        run = self.adapter.headless_run()
        messages = ("You’ve hit your usage limit. Try again at 4:30 PM.",
                    "You’ve hit your usage limit for GPT-5. Switch to another model now, or try again later.",
                    "Your workspace is out of credits. Add credits to continue.",
                    "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue.")
        for kind in ("error", "turn.failed"):
            for message in messages:
                with self.subTest(kind=kind, message=message):
                    record = {"type": kind, "message": message} if kind == "error" else {
                        "type": kind, "error": {"message": message}}
                    self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit(None)])

    def test_arbitrary_error_is_not_usage_limit(self):
        run = self.adapter.headless_run()
        message = "network error mentioning a usage limit"
        self.assertEqual(list(run.parse({"type": "error", "message": message})), [Stderr(message)])
