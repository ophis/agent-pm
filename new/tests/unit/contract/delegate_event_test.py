"""role/contract/delegate_event.py: the callback line, round trips and refusals; its exports."""

import json
import unittest

from agent_pm.role import contract, delegate
from agent_pm.role.contract import KINDS, DelegateEvent, DelegateEventError

from new.tests.case import Case

SID = "1234abcd-0000-4000-8000-000000000000"
BEGUN = DelegateEvent("begun", sid=SID)
PROGRESS = DelegateEvent("progress", name="start", text="planning: 修复\nthe work")
FINISHED = DelegateEvent(
    "finished",
    returncode=0,
    outcome={"status": "done", "title": "T", "summary": "S", "questions": [], "files": ["a"]},
    limit={"resets_at": 1760000000.5},
)
FAILED = DelegateEvent("finished", returncode=1, error="boom", limit={"resets_at": None})
BARE = DelegateEvent("finished", returncode=2)
LIMIT = 'limit: want null or {"resets_at": a number or null}'
OUTCOME = "outcome: want null or an object with a str status"


def line(**data) -> str:
    return json.dumps(data)


class EventLine(Case):
    def refused(self, text, message):
        with self.assertRaises(DelegateEventError) as caught:
            DelegateEvent.parse(text)
        self.assertEqual(str(caught.exception), message)

    def test_each_kind_round_trips(self):
        for event in (BEGUN, PROGRESS, FINISHED, FAILED, BARE):
            with self.subTest(event=event):
                text = event.to_line()
                self.assertEqual(text.splitlines(), [text])
                self.assertEqual(DelegateEvent.parse(text), event)
                self.assertEqual(DelegateEvent.parse(text + "\n"), event)

    def test_the_line_holds_v_kind_and_only_the_kinds_fields(self):
        self.assertEqual(BEGUN.to_line(), f'{{"v":1,"kind":"begun","sid":"{SID}"}}')
        self.assertEqual(
            BARE.to_line(),
            '{"v":1,"kind":"finished","returncode":2,"outcome":null,"error":"","limit":null}',
        )
        self.assertEqual(list(json.loads(PROGRESS.to_line())), ["v", "kind", "name", "text"])
        self.assertEqual(KINDS, ("begun", "progress", "finished"))

    def test_a_finished_line_without_the_optional_fields_takes_their_defaults(self):
        self.assertEqual(DelegateEvent.parse(line(v=1, kind="finished", returncode=2)), BARE)

    def test_kind_must_be_known(self):
        for kind in ("started", None, ["begun"]):
            with self.subTest(kind=kind):
                self.refused(
                    line(v=1, kind=kind, sid=SID), "kind: want one of begun, progress, finished"
                )
        self.refused(line(v=1, sid=SID), "kind: want one of begun, progress, finished")

    def test_v_must_be_the_int_one(self):
        self.refused(line(kind="begun", sid=SID), "v: want 1")
        for v in (2, True, "1", 1.0, None):
            with self.subTest(v=v):
                self.refused(line(v=v, kind="begun", sid=SID), "v: want 1")

    def test_a_field_of_another_kind_is_refused(self):
        self.refused(line(v=1, kind="begun", sid=SID, text="x"), "text: not a begun field")
        self.refused(
            line(v=1, kind="progress", name="n", text="t", sid=SID), "sid: not a progress field"
        )
        self.refused(
            line(v=1, kind="finished", returncode=0, extra=1), "extra: not a finished field"
        )
        for kwargs, field in (
            ({"text": "x"}, "text"),
            ({"error": "x"}, "error"),
            ({"outcome": {}}, "outcome"),
        ):
            with self.subTest(field=field), self.assertRaises(DelegateEventError) as caught:
                DelegateEvent("begun", sid=SID, **kwargs)
            self.assertEqual(str(caught.exception), f"{field}: not a begun field")

    def test_each_kinds_fields_are_checked(self):
        for data, message in (
            ({"kind": "begun"}, "sid: want a non-empty str"),
            ({"kind": "begun", "sid": ""}, "sid: want a non-empty str"),
            ({"kind": "progress", "text": "t"}, "name: want a str"),
            ({"kind": "progress", "name": "n", "text": 1}, "text: want a str"),
            ({"kind": "finished"}, "returncode: want an int"),
            ({"kind": "finished", "returncode": True}, "returncode: want an int"),
            ({"kind": "finished", "returncode": 0.0}, "returncode: want an int"),
            ({"kind": "finished", "returncode": 0, "error": None}, "error: want a str"),
            ({"kind": "finished", "returncode": 0, "outcome": {"title": "T"}}, OUTCOME),
            ({"kind": "finished", "returncode": 0, "outcome": {"status": 1}}, OUTCOME),
            ({"kind": "finished", "returncode": 0, "outcome": ["done"]}, OUTCOME),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": 1, "at": 2}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": True}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": {"resets_at": "soon"}}, LIMIT),
            ({"kind": "finished", "returncode": 0, "limit": 5}, LIMIT),
        ):
            with self.subTest(data=data):
                self.refused(line(v=1, **data), message)

    def test_not_a_json_object_is_refused(self):
        self.refused("[1]", "DelegateEvent line: want a JSON object")
        with self.assertRaises(DelegateEventError) as caught:
            DelegateEvent.parse("{nope")
        self.assertTrue(str(caught.exception).startswith("DelegateEvent line: not JSON ("))
        self.refused(b"{}", "DelegateEvent line: want a str")


class Exports(Case):
    def test_contract_and_delegate_export_the_callback_event(self):
        for name in ("DelegateEvent", "DelegateEventError", "KINDS"):
            with self.subTest(name=name):
                self.assertIn(name, contract.__all__)
        self.assertIn("DelegateEvent", delegate.__all__)
        self.assertIs(delegate.DelegateEvent, DelegateEvent)


if __name__ == "__main__":
    unittest.main()
