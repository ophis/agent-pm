import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
import issues  # noqa: E402

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
HUMANS = ("ann@example.com", "bob@example.com")
URL = "https://linear.app/t/issue/ENG-7"


def comment(body, at, email="ann@example.com", name="Ann", me=False, user=True):
    return {"body": body, "createdAt": at,
            "user": {"email": email, "name": name, "isMe": me} if user else None}


def payload(**over):
    issue = {"id": "uuid-7", "identifier": "ENG-7", "url": URL, "title": "Build it", "description": "Do the thing.",
             "createdAt": "2026-09-01T00:00:00.000Z", "project": {"id": "p-1"},
             "comments": {"nodes": []}, "attachments": {"nodes": []},
             "relations": {"nodes": []}, "inverseRelations": {"nodes": []}}
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
    return issues.Note(body, at, email, name)


def issue(description="", created_at="2026-09-01T00:00:00.000Z", notes=()):
    return issues.Issue("uuid-7", "ENG-7", URL, "T", description, created_at, "p-1", tuple(notes), (), ())


HANDOFF = (f"Handoff from ENG-3: {URL}\n\n"
           "## Source\n- Spec: https://github.com/o/r/blob/main/a.md\n- Plan: https://github.com/o/r/blob/main/b.md\n\n"
           "## Instructions\nAnn, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this.\n\n"
           "## Comments\n- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source\n")


class ReadIssue(unittest.TestCase):
    def test_maps_fields_and_queries_by_identifier(self):
        gql = FakeGql(payload(
            attachments={"nodes": [{"url": "https://x/spec"}, {"url": "https://x/y"}]},
            relations={"nodes": [{"relatedIssue": {"identifier": "ENG-1", "title": "One", "state": {"name": "Done"}}}]},
            inverseRelations={"nodes": [{"issue": {"identifier": "ENG-2", "title": "Two", "state": {"name": "Todo"}}}]}))
        got = issues.read_issue(gql, "ENG-7")
        self.assertEqual(gql.calls, [(issues.Q_ISSUE, {"i": "ENG-7"})])
        self.assertEqual(got, issues.Issue(
            "uuid-7", "ENG-7", URL, "Build it", "Do the thing.", "2026-09-01T00:00:00.000Z", "p-1", (),
            (issues.Link("https://x/spec"), issues.Link("https://x/y")),
            (issues.Linked("ENG-1", "One", "Done"), issues.Linked("ENG-2", "Two", "Todo"))))

    def test_null_description_and_project(self):
        got = read(description=None, project=None)
        self.assertEqual((got.description, got.project_id), ("", None))

    def test_notes_oldest_first(self):
        got = read(comments={"nodes": [
            comment("late", "2026-09-03T00:00:00.000Z", "bob@example.com", "Bob"),
            comment("early", "2026-09-01T12:00:00.000Z"),
            comment("mid", "2026-09-02T00:00:00.000Z", None, None, user=False)]})
        self.assertEqual(got.notes, (
            issues.Note("early", "2026-09-01T12:00:00.000Z", "ann@example.com", "Ann"),
            issues.Note("mid", "2026-09-02T00:00:00.000Z", None, None),
            issues.Note("late", "2026-09-03T00:00:00.000Z", "bob@example.com", "Bob")))

    def test_session_comments_dropped(self):
        session = f"Run {SID} · running · 2026-09-02T00:00:00+08:00\n\n```\ncd /w\n```"
        got = read(comments={"nodes": [
            comment(session, "2026-09-02T00:00:00.000Z", "agent@example.com", "Agent", me=True),
            comment(session, "2026-09-02T01:00:00.000Z"),
            comment("Run notes", "2026-09-02T02:00:00.000Z", me=True)]})
        self.assertEqual([n.at for n in got.notes], ["2026-09-02T01:00:00.000Z", "2026-09-02T02:00:00.000Z"])

    def test_linked_relations_then_inverse_deduped(self):
        def rel(i):
            return {"relatedIssue": {"identifier": i, "title": f"T{i}", "state": {"name": "Todo"}}}

        def inv(i):
            return {"issue": {"identifier": i, "title": f"T{i}", "state": {"name": "Done"}}}
        got = read(relations={"nodes": [rel("ENG-1"), rel("ENG-2"), rel("ENG-1")]},
                   inverseRelations={"nodes": [inv("ENG-2"), inv("ENG-4")]})
        self.assertEqual(got.linked, (issues.Linked("ENG-1", "TENG-1", "Todo"), issues.Linked("ENG-2", "TENG-2", "Todo"),
                                      issues.Linked("ENG-4", "TENG-4", "Done")))

    def test_missing_or_other_issue_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            issues.read_issue(FakeGql({"issue": None}), "ENG-7")
        with self.assertRaises(LookupError):
            read(identifier="ENG-70")

    def test_handoff_property(self):
        self.assertEqual(read(description=HANDOFF).handoff.source, "ENG-3")
        self.assertIsNone(read().handoff)

    def test_frozen(self):
        with self.assertRaises(AttributeError):
            read().title = "x"


class ParseHandoff(unittest.TestCase):
    def test_sections(self):
        """A section starts at a line exactly its header; a missing one is empty; header lines inside Instructions stay
        there; Comments is the last exact header line."""
        inner = "Bob, t:\nFirst.\n## Source\nnot a header\n## Instructions\nnor this\n## Comments\nnor this\n\nMore."
        inside = ("- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments\n", "- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments")
        cases = [
            ("full", HANDOFF, "Ann, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this.",
             "- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source"),
            ("no source, no comments", f"Handoff from ENG-3: {URL}\n\n## Instructions\nGo.\n", "Go.", ""),
            ("header line only", f"Handoff from ENG-3: {URL}", "", ""),
            ("only exact header lines", f"Handoff from ENG-3: {URL}\n\n## Instructions\nSee ## Comments below.\n"
             "## Comments here\n## Comments\n- Ann, t:\n  > x\n", "See ## Comments below.\n## Comments here", "- Ann, t:\n  > x"),
            ("headers inside instructions, with source", f"Handoff from ENG-3: {URL}\n\n## Source\n- Spec: u\n\n"
             f"## Instructions\n{inner}\n\n## Comments\n{inside[0]}", inner, inside[1]),
            ("headers inside instructions, no source", f"Handoff from ENG-3: {URL}\n\n## Instructions\n{inner}\n\n"
             f"## Comments\n{inside[0]}", inner, inside[1]),
            ("comments header is the last exact line", f"Handoff from ENG-3: {URL}\n\n## Source\n- A: u\n\n## Instructions\n"
             "Go.\n## Comments\nx\n\n## Comments\n- Ann, t:\n  > y\n", "Go.\n## Comments\nx", "- Ann, t:\n  > y"),
            ("crlf", f"Handoff from ENG-3: {URL}\r\n\r\n## Source\r\n- A: u\r\n\r\n## Instructions\r\nGo.\r\n", "Go.", ""),
        ]
        for name, description, instructions, comments in cases:
            with self.subTest(name):
                self.assertEqual(issues.parse_handoff(description), issues.Handoff("ENG-3", instructions, comments))

    def test_not_a_handoff(self):
        for description in ("", "Do the thing.", f"\nHandoff from ENG-3: {URL}", f"Handoff from eng-3: {URL}",
                            "Handoff from ENG-3:", f"Handoff from ENG-3: {URL} extra",
                            f"Please see\nHandoff from ENG-3: {URL}\n## Instructions\nGo."):
            with self.subTest(description=description):
                self.assertIsNone(issues.parse_handoff(description))


class IsUser(unittest.TestCase):
    def test_is_user(self):
        for email, want in (("ANN@Example.com", True), ("bob@example.com", True), ("agent@example.com", False),
                            (None, False), ("", False)):
            with self.subTest(email=email):
                self.assertIs(issues.is_user(note("x", email=email), HUMANS), want)


class Brief(unittest.TestCase):
    def test_brief(self):
        """A Handoff's instructions, else the description, `## Comments` and all."""
        for description, want in (
                (HANDOFF, "Ann, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this."),
                (f"Handoff from ENG-3: {URL}", ""),
                ("Do the thing.\n\n## Comments\nx", "Do the thing.\n\n## Comments\nx")):
            with self.subTest(description=description):
                self.assertEqual(issues.brief(issue(description)), want)


class BuildCutoff(unittest.TestCase):
    def test_build_cutoff(self):
        """`at` of the latest (by time) non-user note whose stripped body starts `Build started` as a word, else the
        creation time."""
        created = "2026-09-01T00:00:00.000Z"
        cases = [
            ("no notes", (), created),
            ("no match", (note("Working on it"),), created),
            ("latest non-user match", (note("Build started: a", "2026-09-02T00:00:00.000Z"),
                                       note("Build started: b", "2026-09-04T00:00:00.000Z"),
                                       note("Build ready: c", "2026-09-05T00:00:00.000Z")), "2026-09-04T00:00:00.000Z"),
            ("latest by time, not position", (note("Build started: b", "2026-09-04T00:00:00.000Z"),
                                              note("Build started: a", "2026-09-02T00:00:00.000Z")), "2026-09-04T00:00:00.000Z"),
            ("user notes ignored", (note("Build started: a", "2026-09-02T00:00:00.000Z"),
                                    note("Build started", "2026-09-03T00:00:00.000Z", email="ANN@example.com")),
             "2026-09-02T00:00:00.000Z"),
            ("body stripped", (note("\n  Build started: x\n", "2026-09-02T00:00:00.000Z"),), "2026-09-02T00:00:00.000Z"),
            ("bare", (note("Build started", "2026-09-02T00:00:00.000Z"),), "2026-09-02T00:00:00.000Z"),
            *((body, (note(body, "2026-09-02T00:00:00.000Z"),), created)
              for body in ("Build startedness", "Build starting", "build started", "Re: Build started: x", "Build  started")),
        ]
        for name, notes, want in cases:
            with self.subTest(name):
                self.assertEqual(issues.build_cutoff(issue(created_at=created, notes=notes), HUMANS), want)


if __name__ == "__main__":
    unittest.main()
