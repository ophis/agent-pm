"""Issue reading and the Handoff reader-side helpers' boundary behavior."""

import dataclasses

from agent_pm.linear_orchestrator.engine import config, issues
from agent_pm.linear_orchestrator.flow import handoff
from agent_pm.linear_orchestrator.flow.types import (
    Issue,
    Link,
    Linked,
    Note,
)
from orchestrator_tests.case import Case

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

HUMANS = ("ann@example.com", "bob@example.com")

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
    return issues.read_issue(FakeGql(payload(**over)), "ENG-7")


def note(body, at="2026-09-02T00:00:00.000Z", email="agent@example.com", name="Agent"):
    return Note(body, at, email, name)


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
        got = issues.read_issue(gql, "ENG-7")
        self.assertEqual(gql.calls, [(issues.Q_ISSUE, {"i": "ENG-7"})])
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
        self.assertIn("labels(first: 50) { nodes { id } }", issues.Q_ISSUE)

    def test_null_description_and_project(self):
        got = read(description=None, project=None)
        self.assertEqual((got.description, got.project_id), ("", None))

    def test_notes_oldest_first(self):
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
                Note("early", "2026-09-01T12:00:00.000Z", "ann@example.com", "Ann"),
                Note("mid", "2026-09-02T00:00:00.000Z", None, None),
                Note("late", "2026-09-03T00:00:00.000Z", "bob@example.com", "Bob"),
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
            issues.read_issue(FakeGql({"issue": None}), "ENG-7")
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
        for email, want in (
            ("ANN@Example.com", True),
            ("bob@example.com", True),
            ("agent@example.com", False),
            (None, False),
            ("", False),
        ):
            with self.subTest(email=email):
                self.assertIs(issues.is_user(note("x", email=email), HUMANS), want)


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
    def test_build_cutoff(self):
        created = "2026-09-01T00:00:00.000Z"
        cases = [
            ("no notes", (), created),
            ("no match", (note("Working on it"),), created),
            (
                "latest non-user match",
                (
                    note("Build started: a", "2026-09-02T00:00:00.000Z"),
                    note("Build started: b", "2026-09-04T00:00:00.000Z"),
                    note("Build ready: c", "2026-09-05T00:00:00.000Z"),
                ),
                "2026-09-04T00:00:00.000Z",
            ),
            (
                "latest by time, not position",
                (
                    note("Build started: b", "2026-09-04T00:00:00.000Z"),
                    note("Build started: a", "2026-09-02T00:00:00.000Z"),
                ),
                "2026-09-04T00:00:00.000Z",
            ),
            (
                "user notes ignored",
                (
                    note("Build started: a", "2026-09-02T00:00:00.000Z"),
                    note("Build started", "2026-09-03T00:00:00.000Z", email="ANN@example.com"),
                ),
                "2026-09-02T00:00:00.000Z",
            ),
            (
                "body stripped",
                (note("\n  Build started: x\n", "2026-09-02T00:00:00.000Z"),),
                "2026-09-02T00:00:00.000Z",
            ),
            (
                "bare",
                (note("Build started", "2026-09-02T00:00:00.000Z"),),
                "2026-09-02T00:00:00.000Z",
            ),
            *(
                (body, (note(body, "2026-09-02T00:00:00.000Z"),), created)
                for body in (
                    "Build startedness",
                    "Build starting",
                    "build started",
                    "Re: Build started: x",
                    "Build  started",
                )
            ),
        ]
        for name, notes, want in cases:
            with self.subTest(name):
                self.assertEqual(
                    issues.build_cutoff(issue(created_at=created, notes=notes), HUMANS), want
                )

    def test_the_workflow_build_start_lead_is_the_cutoff(self):
        created, at = "2026-09-01T00:00:00.000Z", "2026-09-02T00:00:00.000Z"
        [start] = [t.start for t in config.TASKS.values() if t.kind == "build"]
        notes = (note(f"{start} first build of the PRD", at),)
        self.assertEqual(issues.build_cutoff(issue(created_at=created, notes=notes), HUMANS), at)
