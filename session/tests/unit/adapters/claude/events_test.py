"""events test behavior."""

import json

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.engine.types import (
    Activity,
    LimitHit,
    Reply,
    SessionBound,
    TurnEnd,
    Usage,
    Window,
)

from session.tests.case import Case
from session.tests.unit.adapters.claude.fixtures import (
    INIT,
    SID,
    Base,
    assistant,
    event,
    info_line,
    rejected,
    text,
    tool,
    unified,
    usage,
)
from session.tests.unit.adapters.claude.fixtures import (
    line as stream_line,
)


class ParseTest(Base):
    def setUp(self):
        super().setUp()
        self.run = ClaudeAdapter({}).headless()

    def parse(self, *records):
        return [event for record in records for event in self.run.parse(record)]

    def test_a_session_in_order(self):
        lines = [
            stream_line(INIT),
            "not json",
            stream_line(assistant(text("one"))),
            stream_line(assistant(text("two"))),
            stream_line({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
            "[1]",
            stream_line(assistant(text("three"))),
            stream_line({"type": "result", "subtype": "success", "result": "three"}),
        ]
        self.assertEqual(
            list(self.run.events(lines)),
            [SessionBound(SID), Reply("one"), Reply("two"), Reply("three"), TurnEnd("three", 0)],
        )

    def test_init_binds_its_session_id(self):
        self.assertEqual(self.parse(INIT), [SessionBound(SID)])
        for record in (
            {**INIT, "subtype": "other"},
            {**INIT, "session_id": ""},
            {**INIT, "session_id": 1},
            {"type": "system", "subtype": "init"},
        ):
            with self.subTest(record=record):
                self.assertEqual(self.parse(record), [])

    def test_text_blocks_joined_blank_none(self):
        self.assertEqual(self.parse(assistant(text("a"), text("b"))), [Reply("a\nb")])
        self.assertEqual(self.parse(assistant(text(" \n"), text(""))), [])
        self.assertEqual(self.parse(assistant()), [])

    def test_tool_use_is_activity_after_the_reply(self):
        self.assertEqual(
            self.parse(assistant(tool("Bash"), text("x"), tool("Read"))),
            [Reply("x"), Activity("Bash"), Activity("Read")],
        )

    def test_result_is_the_turn_end(self):
        self.assertEqual(self.parse({"type": "result", "result": "done"}), [TurnEnd("done", 0)])
        self.assertEqual(
            self.parse({"type": "result", "result": None}, {"type": "result"}),
            [TurnEnd("", 0), TurnEnd("", 0)],
        )

    def test_other_records_nothing(self):
        self.assertEqual(
            self.parse({"type": "user"}, {"type": "stream_event"}, {}, {"type": 1}), []
        )

    def test_wrong_typed_fields_are_absent(self):
        rows = (
            ({"type": "assistant", "message": "x"}, []),
            ({"type": "assistant", "message": {"content": "x"}}, []),
            ({"type": "assistant"}, []),
            (assistant("x", 1, None, text("y")), [Reply("y")]),
            (assistant(text(1), {"type": "text"}), []),
            (assistant({"type": "tool_use", "name": 1}), [Activity("")]),
            (assistant({"type": "tool_use"}), [Activity("")]),
            ({"type": "rate_limit_event", "rate_limit_info": "rejected"}, []),
            ({"type": "rate_limit_event"}, []),
        )
        for record, events in rows:
            with self.subTest(record=record):
                self.assertEqual(list(ClaudeAdapter({}).headless().parse(record)), events)

    def test_a_rejected_rate_limit_event_is_a_limit_hit(self):
        self.assertEqual(self.parse(rejected(1700000000)), [LimitHit(1700000000.0)])
        for record in (rejected(status="allowed"), rejected(status="allowed_warning")):
            with self.subTest(record=record):
                self.assertEqual(list(ClaudeAdapter({}).headless().parse(record)), [])
        for resets in (None, True, "1700000000", 1e999):
            with self.subTest(resets=resets):
                self.assertEqual(
                    list(ClaudeAdapter({}).headless().parse(rejected(resets))),
                    [LimitHit(None)],
                )

    def test_an_assistant_rate_limit_error_is_one_limit_hit_with_the_remembered_reset(self):
        self.assertEqual(
            self.parse(rejected(5), assistant(text("limit"), error="rate_limit"), rejected(6)),
            [LimitHit(5.0), Reply("limit")],
        )
        run = ClaudeAdapter({}).headless()
        self.assertEqual(
            [
                e
                for r in (
                    assistant(text("limit"), error="rate_limit"),
                    assistant(error="rate_limit"),
                    rejected(7),
                )
                for e in run.parse(r)
            ],
            [LimitHit(None), Reply("limit")],
        )
        self.assertEqual(list(ClaudeAdapter({}).headless().parse(assistant(error="other"))), [])

    def test_each_run_sends_its_own(self):
        adapter = ClaudeAdapter({})
        first, second = adapter.headless(), adapter.headless()
        self.assertIsNot(first, second)
        self.assertEqual(list(first.parse(rejected(1))), [LimitHit(1.0)])
        self.assertEqual(list(second.parse(assistant(error="rate_limit"))), [LimitHit(None)])


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
