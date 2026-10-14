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


class Codec(Case):
    def test_each_actual_domain_event_preserves_its_fields_in_one_line(self):
        outcome = Outcome(OutcomeStatus.DONE, "Title", "Summary", deliverable="# Body\n")
        events = (
            (Begun(SID), DelegateEvent("begun", sid=SID)),
            (Progress("draft", "one\n二"), DelegateEvent("progress", name="draft", text="one\n二")),
            (
                Finished(Result(7, outcome, "exit diagnostic")),
                DelegateEvent(
                    "finished", returncode=7, outcome=asdict(outcome), error="exit diagnostic"
                ),
            ),
            (
                Finished(Result(0, None, "usage limit", Limit(1767225600.0))),
                DelegateEvent(
                    "finished", returncode=0, error="usage limit", limit={"resets_at": 1767225600.0}
                ),
            ),
        )
        for event, expected in events:
            with self.subTest(event=event):
                encoded = EventCallback.encode(event)
                self.assertEqual(encoded, expected)
                line = encoded.to_line()
                self.assertEqual(line.splitlines(), [line])
                self.assertEqual(DelegateEvent.parse(line), expected)
