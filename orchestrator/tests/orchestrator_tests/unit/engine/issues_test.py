"""Issue reading and the Handoff reader-side helpers' boundary behavior."""

import dataclasses

from agent_pm.orchestrator.engine import config, issues
from agent_pm.orchestrator.engine.flow import handoff
from agent_pm.orchestrator.engine.flow.types import (
    Issue,
    Link,
    Linked,
    Note,
)
from agent_pm.orchestrator.trackers.linear import linear_tracker
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import PACKAGED_FLOW, workflow_file

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

SELVES = frozenset({"agent@example.com"})

URL = "https://linear.app/t/issue/ENG-7"


def comment(body, at, *, email="ann@example.com", name="Ann", me=False, user=True):
    return {
        "body": body,
        "createdAt": at,
        "user": {"email": email, "name": name, "isMe": me} if user else None,
    }


def payload(**over):
    issue = {
        "id": "uuid-7",
        "identifier": "ENG-7",
        "url": URL,
        "title": "Build it",
        "description": "Do the thing.",
        "createdAt": "2026-09-01T00:00:00.000Z",
        "project": {"id": "p-1"},
        "comments": {"nodes": []},
        "attachments": {"nodes": []},
        "relations": {"nodes": []},
        "inverseRelations": {"nodes": []},
        "labels": {"nodes": []},
    }
    issue.update(over)
    return {"issue": issue}


class FakeGql:
    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, query, **v):
        self.calls.append((query, v))
        return self.response


def read(**over):
    return LinearTracker(FakeGql(payload(**over)), selves=SELVES).issue("ENG-7")


def note(
    body, at="2026-09-02T00:00:00.000Z", email="agent@example.com", name="Agent", origin="self"
):
    return Note(body, at, email, name, origin)


def issue(description="", created_at="2026-09-01T00:00:00.000Z", notes=()):
    return Issue(
        "uuid-7",
        "ENG-7",
        URL,
        "T",
        description,
        created_at,
        "p-1",
        tuple(notes),
        (),
        (),
        handoff=handoff.parse_handoff(description),
    )


HANDOFF = (
    f"Handoff from ENG-3: {URL}\n\n"
    "## Source\n- Spec: https://github.com/o/r/blob/main/a.md\n- Plan: https://github.com/o/r/blob/main/b.md\n\n"
    "## Instructions\nAnn, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this.\n\n"
    "## Comments\n- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source\n"
)


class ReadIssue(Case):
    def test_maps_fields_and_queries_by_identifier(self):
        gql = FakeGql(
            payload(
                attachments={"nodes": [{"url": "https://x/spec"}, {"url": "https://x/y"}]},
                relations={
                    "nodes": [
                        {
                            "relatedIssue": {
                                "identifier": "ENG-1",
                                "title": "One",
                                "state": {"name": "Done"},
                            }
                        }
                    ]
                },
                inverseRelations={
                    "nodes": [
                        {
                            "issue": {
                                "identifier": "ENG-2",
                                "title": "Two",
                                "state": {"name": "Todo"},
                            }
                        }
                    ]
                },
            )
        )
        got = LinearTracker(gql, selves=SELVES).issue("ENG-7")
        self.assertEqual(gql.calls, [(linear_tracker.Q_READ, {"i": "ENG-7"})])
        self.assertEqual(
            got,
            Issue(
                "uuid-7",
                "ENG-7",
                URL,
                "Build it",
                "Do the thing.",
                "2026-09-01T00:00:00.000Z",
                "p-1",
                (),
                (Link("https://x/spec"), Link("https://x/y")),
                (Linked("ENG-1", "One", "Done"), Linked("ENG-2", "Two", "Todo")),
                handoff=handoff.parse_handoff("Do the thing."),
            ),
        )

    def test_read_issue_labels(self):
        self.assertEqual(read().labels, ())
        got = read(labels={"nodes": [{"id": "l-2"}, {"id": "l-1"}]})
        self.assertEqual(got.labels, ("l-2", "l-1"))
        self.assertIn("labels(first: 50) { nodes { id } }", linear_tracker.Q_READ)

    def test_null_description_and_project(self):
        got = read(description=None, project=None)
        self.assertEqual((got.description, got.home), ("", None))

    def test_notes_oldest_first_userless_dropped(self):
        got = read(
            comments={
                "nodes": [
                    comment(
                        "late",
                        "2026-09-03T00:00:00.000Z",
                        email="bob@example.com",
                        name="Bob",
                    ),
                    comment("early", "2026-09-01T12:00:00.000Z"),
                    comment(
                        "mid",
                        "2026-09-02T00:00:00.000Z",
                        email=None,
                        name=None,
                        user=False,
                    ),
                ]
            }
        )
        self.assertEqual(
            got.notes,
            (
                Note("early", "2026-09-01T12:00:00.000Z", "ann@example.com", "Ann", "other"),
                Note("late", "2026-09-03T00:00:00.000Z", "bob@example.com", "Bob", "other"),
            ),
        )

    def test_session_comments_dropped(self):
        session = f"Run {SID} · running · 2026-09-02T00:00:00+08:00\n\n```\ncd /w\n```"
        got = read(
            comments={
                "nodes": [
                    comment(
                        session,
                        "2026-09-02T00:00:00.000Z",
                        email="agent@example.com",
                        name="Agent",
                        me=True,
                    ),
                    comment(session, "2026-09-02T01:00:00.000Z"),
                    comment("Run notes", "2026-09-02T02:00:00.000Z", me=True),
                ]
            }
        )
        self.assertEqual(
            [n.at for n in got.notes], ["2026-09-02T01:00:00.000Z", "2026-09-02T02:00:00.000Z"]
        )

    def test_linked_relations_then_inverse_deduped(self):
        def rel(i):
            return {"relatedIssue": {"identifier": i, "title": f"T{i}", "state": {"name": "Todo"}}}

        def inv(i):
            return {"issue": {"identifier": i, "title": f"T{i}", "state": {"name": "Done"}}}

        got = read(
            relations={"nodes": [rel("ENG-1"), rel("ENG-2"), rel("ENG-1")]},
            inverseRelations={"nodes": [inv("ENG-2"), inv("ENG-4")]},
        )
        self.assertEqual(
            got.linked,
            (
                Linked("ENG-1", "TENG-1", "Todo"),
                Linked("ENG-2", "TENG-2", "Todo"),
                Linked("ENG-4", "TENG-4", "Done"),
            ),
        )

    def test_missing_or_other_issue_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            LinearTracker(FakeGql({"issue": None}), selves=SELVES).issue("ENG-7")
        with self.assertRaises(LookupError):
            read(identifier="ENG-70")

    def test_handoff_property(self):
        self.assertEqual(read(description=HANDOFF).handoff.source, "ENG-3")
        self.assertIsNone(read().handoff)

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            read().title = "x"


class IsUser(Case):
    def test_is_user(self):
        for origin, want in (("other", True), ("self", False)):
            with self.subTest(origin=origin):
                self.assertIs(issues.is_user(note("x", origin=origin)), want)


class Brief(Case):
    def test_brief(self):
        for description, want in (
            (
                HANDOFF,
                (
                    "Ann, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this."
                ),
            ),
            (f"Handoff from ENG-3: {URL}", ""),
            ("Do the thing.\n\n## Comments\nx", "Do the thing.\n\n## Comments\nx"),
        ):
            with self.subTest(description=description):
                self.assertEqual(issues.brief(issue(description)), want)


class BuildCutoff(Case):
    CREATED = "2026-09-01T00:00:00.000Z"
    START = "2026-10-22T08:44:37.900Z"
    DONE = "2026-10-22T08:45:00.000Z"
    NEXT = "2026-10-22T09:14:37.900Z"

    def cutoff(self, *notes):
        return issues.build_cutoff(issue(created_at=self.CREATED, notes=notes))

    def test_a_start_without_delivery_does_not_consume_comments(self):
        for body in ("Build started", "Build started: light-build", "\n Build started: x\n"):
            with self.subTest(body=body):
                self.assertEqual(self.cutoff(note(body, self.START)), self.CREATED)

    def test_only_successful_build_delivery_qualifies_a_start(self):
        for body in ("Build ready", "Build ready: done", "\n Build ready: done\n"):
            with self.subTest(body=body):
                self.assertEqual(
                    self.cutoff(note("Build started", self.START), note(body, self.DONE)),
                    self.START,
                )
        for body in ("Build failed: no", "Question: which?", "Build readyness", "build ready"):
            with self.subTest(body=body):
                self.assertEqual(
                    self.cutoff(note("Build started", self.START), note(body, self.DONE)),
                    self.CREATED,
                )

    def test_later_unfinished_build_keeps_the_previous_delivered_boundary(self):
        self.assertEqual(
            self.cutoff(
                note("Build started: next", self.NEXT),
                note("Build ready: done", self.DONE),
                note("Build started: first", self.START),
            ),
            self.START,
        )

    def test_latest_delivered_start_wins_by_time_not_position_or_iso_spelling(self):
        self.assertEqual(
            self.cutoff(
                note("Build ready", "2026-10-22T11:20:00+02:00"),
                note("Build started: next", self.NEXT),
                note("Build ready", self.DONE),
                note("Build started: first", self.START),
            ),
            self.NEXT,
        )

    def test_human_lookalikes_and_other_stages_do_not_qualify_a_build(self):
        for start, done in (
            (note("Build started", self.START, origin="other"), note("Build ready", self.DONE)),
            (note("Build started", self.START), note("Build ready", self.DONE, origin="other")),
            (note("Research started", self.START), note("Build ready", self.DONE)),
            (note("Build startedness", self.START), note("Build ready", self.DONE)),
            (note("Build started", self.START), note("Research ready", self.DONE)),
        ):
            with self.subTest(start=start, done=done):
                self.assertEqual(self.cutoff(start, done), self.CREATED)

    def test_completion_without_a_preceding_start_has_no_boundary(self):
        self.assertEqual(self.cutoff(note("Build ready", self.DONE)), self.CREATED)
        self.assertEqual(
            self.cutoff(note("Build ready", self.START), note("Build started", self.DONE)),
            self.CREATED,
        )

    def test_leads_come_from_the_loaded_workflow(self):
        config.TASKS.load(
            workflow_file(
                PACKAGED_FLOW.replace('start = "Build started:"', 'start = "Coding:"').replace(
                    'done = "Build ready:"', 'done = "Delivered:"'
                )
            )
        )
        self.assertEqual(
            self.cutoff(note("Coding: go", self.START), note("Delivered: done", self.DONE)),
            self.START,
        )
        self.assertEqual(
            self.cutoff(note("Build started", self.START), note("Build ready", self.DONE)),
            self.CREATED,
        )

    def test_empty_start_or_done_lead_cannot_qualify_a_boundary(self):
        for lead in ('start = "Build started:"', 'done = "Build ready:"'):
            with self.subTest(lead=lead):
                config.TASKS.load(
                    workflow_file(PACKAGED_FLOW.replace(lead, lead.split("=")[0] + '= ""'))
                )
                self.assertEqual(
                    self.cutoff(note("Build started", self.START), note("Build ready", self.DONE)),
                    self.CREATED,
                )
