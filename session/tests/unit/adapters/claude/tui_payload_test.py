"""tui payload test behavior."""

import json
import os
import time
from unittest import mock

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    LimitHit,
    SessionBound,
    TurnEnd,
)

from session.tests.case import Case
from session.tests.unit.adapters.claude.fixtures import (
    NOW,
    Base,
    epoch,
    said,
    text,
    tool,
)


class TuiPayloadTest(Base):
    def setUp(self):
        super().setUp()
        self.run = ClaudeAdapter({}).tui()
        self.path = os.path.join(self.tmp, "t.jsonl")

    def transcript(self, *records):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("".join(f"{r if isinstance(r, str) else json.dumps(r)}\n" for r in records))

    def test_the_last_assistant_message_first(self):
        self.transcript(said("m1", text("old")))
        self.assertEqual(
            self.run.reply({"last_assistant_message": "new", "transcript_path": self.path}), "new"
        )
        self.assertEqual(
            self.run.reply({"last_assistant_message": "", "transcript_path": self.path}), ""
        )

    def test_else_the_transcripts_last_assistant_message(self):
        self.transcript(
            {"type": "user", "message": {"content": "q"}},
            said("m1", text("old")),
            said("m2", text("first")),
            said("m2", tool("Bash")),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            said("m2", text("second")),
            said("m3", tool("Read")),
            "not json",
            "[1]",
            "",
        )
        for extra in ({}, {"last_assistant_message": None}, {"last_assistant_message": 1}):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.run.reply({"transcript_path": self.path, **extra}), "first\nsecond"
                )

    def test_a_record_without_an_id_is_its_own_message(self):
        self.transcript(said(None, text("x")), said(None, text("y")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")
        self.transcript(
            said("m1", text("x")),
            {"type": "assistant", "message": {"id": 1, "content": [text("y")]}},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")

    def test_wrong_typed_records_are_skipped(self):
        self.transcript(
            said("m1", text("ok")),
            {"type": "assistant", "message": "x"},
            {"type": "assistant", "message": {"content": "x"}},
            {"type": "assistant"},
            said("m2", text(1), {"type": "text"}, "x", 1),
            {"type": "user", "message": said("m3", text("u"))},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "ok")

    def test_only_the_last_4_mib_is_read(self):
        self.transcript(
            said("m1", text("early")), {"type": "user", "message": {"content": "x" * (5 << 20)}}
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "")
        self.transcript(said("m1", text("x" * (5 << 20))), said("m2", text("late")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "late")

    def test_no_readable_transcript_is_empty(self):
        self.transcript(said("m1", text("ok")))
        link = os.path.join(self.tmp, "link.jsonl")
        os.symlink(self.path, link)
        fifo = os.path.join(self.tmp, "fifo.jsonl")
        os.mkfifo(fifo)
        for path in (
            link,
            fifo,
            self.tmp,
            os.path.join(self.tmp, "absent.jsonl"),
            "",
            1,
            None,
            "a\0b",
            "\ud800",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.run.reply({"transcript_path": path}), "")
        self.assertEqual(self.run.reply({}), "")

    def test_pending_activity_and_blocked_reason(self):
        rows = (
            (self.run.pending, {"background_tasks": [1, {}]}, 2),
            (self.run.pending, {"background_tasks": []}, 0),
            (self.run.pending, {}, 0),
            (self.run.pending, {"background_tasks": 3}, 0),
            (self.run.pending, {"background_tasks": "ab"}, 0),
            (self.run.pending, {"background_tasks": {"a": 1}}, 0),
            (self.run.activity, {"tool_name": "Bash"}, "Bash"),
            (self.run.activity, {}, ""),
            (self.run.activity, {"tool_name": 1}, ""),
            (self.run.blocked_reason, {"tool_name": "Bash", "message": "m"}, "Bash"),
            (
                self.run.blocked_reason,
                {"message": "Claude needs your permission"},
                "Claude needs your permission",
            ),
            (self.run.blocked_reason, {"tool_name": 1, "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": "", "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": None, "message": 2}, ""),
            (self.run.blocked_reason, {}, ""),
        )
        for row, (call, payload, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(call(payload), want)

    def test_hook_events_by_label(self):
        rows = (
            (
                "Stop",
                {"session_id": "n1", "last_assistant_message": "hi", "background_tasks": [1, 2]},
                [SessionBound("n1"), TurnEnd("hi", 2)],
            ),
            ("PermissionRequest", {"session_id": "n0", "tool_name": "Bash"}, [Blocked("Bash")]),
            ("Notification", {"message": "Claude is waiting"}, [Blocked("Claude is waiting")]),
            ("PostToolUse", {"tool_name": "Edit"}, [Activity("Edit")]),
            ("UserPromptSubmit", {"prompt": "x"}, [Activity("")]),
            ("SessionStart", {"session_id": "n0"}, []),
        )
        for label, payload, events in rows:
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload, "n0"), events)

    def test_the_trust_dialog(self):
        self.assertEqual(ClaudeTui.dialogs, ("Quick safety check",))


class LimitTest(Case):
    def limit(self, screen, now=NOW):
        with mock.patch("time.time", return_value=now):
            return ClaudeAdapter({}).tui().limit(screen)

    def check(self, rows):
        for screen, resets in rows:
            with self.subTest(screen=screen):
                self.assertEqual(self.limit(screen), LimitHit(resets))

    def test_the_limit_messages_and_their_reset(self):
        self.check(
            (
                (
                    "You've hit your session limit · resets 3pm (America/Los_Angeles)",
                    epoch("2026-10-11 15:00"),
                ),
                (
                    "You've hit your session limit · resets 9:30am (America/Los_Angeles)",
                    epoch("2026-10-12 09:30"),
                ),
                (
                    (
                        "You've hit your weekly limit · resets Oct 15, 9am (America/"
                        "Los_Angeles) · progress saved"
                    ),
                    epoch("2026-10-15 09:00"),
                ),
                (
                    "You've hit your Opus limit · resets Jan 2, 2027, 12am (Europe/London)",
                    epoch("2027-01-02 00:00", "Europe/London"),
                ),
                (
                    "You've hit your usage limit · resets 12pm (UTC)",
                    epoch("2026-10-12 12:00", "UTC"),
                ),
                (
                    (
                        "You've hit your org's monthly spend limit · your session limit "
                        "resets 4pm (Asia/Tokyo)"
                    ),
                    epoch("2026-10-12 16:00", "Asia/Tokyo"),
                ),
                (
                    "You've hit your limit · resets 11pm (Etc/GMT+5)",
                    epoch("2026-10-11 23:00", "Etc/GMT+5"),
                ),
                (
                    "Usage limit reached · resets 3pm (America/New_York)",
                    epoch("2026-10-11 15:00", "America/New_York"),
                ),
                (
                    (
                        "> go on\n\n  ⎿  You've hit your\n   session  limit ·\n resets 3pm"
                        "\n  (America/Los_Angeles)\n"
                    ),
                    epoch("2026-10-11 15:00"),
                ),
            )
        )

    def test_a_weekday_is_the_next_such_day(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        self.check(
            (
                (week.format("Monday 9am"), epoch("2026-10-12 09:00")),
                (week.format("Sun 11am"), epoch("2026-10-11 11:00")),
                (week.format("Sunday, 9am"), epoch("2026-10-18 09:00")),
                (week.format("sat 9am"), epoch("2026-10-17 09:00")),
            )
        )

    def test_a_year_less_date_over_a_day_past_is_next_years(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        for now, when, resets in (
            ("2026-12-30 10:00", "Jan 2, 9am", "2027-01-02 09:00"),
            ("2026-01-01 10:00", "Jan 2, 9am", "2026-01-02 09:00"),
            ("2026-12-30 10:00", "Dec 30, 9am", "2026-12-30 09:00"),
            ("2026-12-30 10:00", "Dec 29, 9am", "2027-12-29 09:00"),
            ("2026-12-30 10:00", "Jan 2, 2026, 9am", "2026-01-02 09:00"),
        ):
            with self.subTest(now=now, when=when):
                self.assertEqual(self.limit(week.format(when), epoch(now)), LimitHit(epoch(resets)))

    def test_no_known_zone_is_local_time(self):
        self.addCleanup(time.tzset)
        patch = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        patch.start()
        self.addCleanup(patch.stop)
        time.tzset()
        today = epoch("2026-10-12 15:00", "Asia/Tokyo")
        self.check(
            (
                ("You've hit your session limit · resets 3pm", today),
                ("You've hit your session limit · resets 3pm (Mars/Olympus)", today),
                ("You've hit your session limit · resets 3pm (America)", today),
                ("You've hit your session limit · resets 3pm (../../etc/passwd)", today),
                ("You've hit your session limit · resets 3pm ()", today),
                ("You've hit your session limit · resets 3pm (A/B/C/D)", today),
                (
                    "You've hit your weekly limit · resets Oct 15, 9am",
                    epoch("2026-10-15 09:00", "Asia/Tokyo"),
                ),
            )
        )

    def test_the_last_message_counts(self):
        session = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
        self.check(
            (
                (
                    (
                        f"{session}"
                        f" … You've hit your weekly limit · resets Oct 15, 9am (America/"
                        f"Los_Angeles)"
                    ),
                    epoch("2026-10-15 09:00"),
                ),
                (f"{session} You've hit your fast limit · resets in 5m", epoch("2026-10-11 15:00")),
            )
        )

    def test_an_unparsable_reset_is_none(self):
        self.check(
            (
                ("You've hit your session limit", None),
                ("Usage limit reached · continuing automatically in 2h · esc to cancel", None),
                ("You've hit your session limit · resets soon", None),
                ("You've hit your session limit · resets 13pm (UTC)", None),
                ("You've hit your session limit · resets 0am (UTC)", None),
                ("You've hit your session limit · resets 3:75pm (UTC)", None),
                ("You've hit your weekly limit · resets Feb 30, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Monday 15, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Smarch 3, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Funday 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Oct 3pm (UTC)", None),
            )
        )

    def test_no_limit_message_is_none(self):
        for screen in (
            "",
            "Welcome back",
            "You've hit your fast limit · resets in 5m",
            "You've used 90% of your session limit · resets 3pm (America/Los_Angeles)",
            "Approaching weekly limit",
            "Your usage limit has reset",
            "usage limit reached",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(self.limit(screen))
