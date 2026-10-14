"""Domain events encode through the existing one-line DelegateEvent contract."""

from dataclasses import asdict

from agent_pm.delegate import (
    Begun,
    DelegateEvent,
    Finished,
    Limit,
    Outcome,
    OutcomeStatus,
    Progress,
    Result,
)
from agent_pm.delegate.engine.callback import EventCallback

from delegate.tests.case import Case
from delegate.tests.harness import SID

RUN = {"sid": SID, "attempt": 3}


class Codec(Case):
    def test_each_actual_domain_event_preserves_its_fields_in_one_line_stamped_with_its_run(self):
        outcome = Outcome(OutcomeStatus.DONE, "Title", "Summary", deliverable="# Body\n")
        events = (
            (Begun(SID), DelegateEvent("begun", **RUN)),
            (
                Progress("draft", "one\n二"),
                DelegateEvent("progress", **RUN, name="draft", text="one\n二"),
            ),
            (
                Finished(Result(7, outcome, "exit diagnostic")),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=7,
                    outcome=asdict(outcome),
                    error="exit diagnostic",
                ),
            ),
            (
                Finished(Result(0, None, "usage limit", Limit(1767225600.0))),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=0,
                    error="usage limit",
                    limit={"resets_at": 1767225600.0},
                ),
            ),
        )
        callback = EventCallback(("record",), "/unused", **RUN)
        for event, expected in events:
            with self.subTest(event=event):
                encoded = callback.encode(event)
                self.assertEqual(encoded, expected)
                line = encoded.to_line()
                self.assertEqual(line.splitlines(), [line])
                self.assertEqual(DelegateEvent.parse(line), expected)
