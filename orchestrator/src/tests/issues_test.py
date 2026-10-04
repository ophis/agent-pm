import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

    def test_missing_issue_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            issues.read_issue(FakeGql({"issue": None}), "ENG-7")

    def test_other_identifier_raises_lookup_error(self):
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
        self.assertEqual(issues.parse_handoff(HANDOFF), issues.Handoff(
            "ENG-3",
            "Ann, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this.",
            "- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source"))

    def test_missing_sections_are_empty(self):
        got = issues.parse_handoff(f"Handoff from ENG-3: {URL}\n\n## Instructions\nGo.\n")
        self.assertEqual(got, issues.Handoff("ENG-3", "Go.", ""))
        got = issues.parse_handoff(f"Handoff from ENG-3: {URL}")
        self.assertEqual(got, issues.Handoff("ENG-3", "", ""))

    def test_only_exact_header_lines_start_a_section(self):
        got = issues.parse_handoff(f"Handoff from ENG-3: {URL}\n\n## Instructions\nSee ## Comments below.\n## Comments here\n"
                                   "## Comments\n- Ann, t:\n  > x\n")
        self.assertEqual((got.instructions, got.comments),
                         ("See ## Comments below.\n## Comments here", "- Ann, t:\n  > x"))

    def test_header_lines_inside_instructions_stay_in_instructions(self):
        text = ("Bob, t:\nFirst.\n## Source\nnot a header\n## Instructions\nnor this\n## Comments\nnor this\n\nMore.")
        for sources in ("## Source\n- Spec: u\n\n", ""):
            with self.subTest(sources=sources):
                got = issues.parse_handoff(f"Handoff from ENG-3: {URL}\n\n{sources}## Instructions\n{text}\n\n"
                                           "## Comments\n- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments\n")
                self.assertEqual(got.instructions, text.strip())
                self.assertEqual(got.comments, "- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments")

    def test_comments_header_is_the_last_exact_line(self):
        got = issues.parse_handoff(f"Handoff from ENG-3: {URL}\n\n## Source\n- A: u\n\n## Instructions\nGo.\n## Comments\nx\n\n"
                                   "## Comments\n- Ann, t:\n  > y\n")
        self.assertEqual((got.instructions, got.comments), ("Go.\n## Comments\nx", "- Ann, t:\n  > y"))

    def test_crlf(self):
        got = issues.parse_handoff(f"Handoff from ENG-3: {URL}\r\n\r\n## Source\r\n- A: u\r\n\r\n## Instructions\r\nGo.\r\n")
        self.assertEqual(got.instructions, "Go.")

    def test_not_a_handoff(self):
        for description in ("", "Do the thing.", f"\nHandoff from ENG-3: {URL}", f"Handoff from eng-3: {URL}",
                            "Handoff from ENG-3:", f"Handoff from ENG-3: {URL} extra",
                            f"Please see\nHandoff from ENG-3: {URL}\n## Instructions\nGo."):
            with self.subTest(description=description):
                self.assertIsNone(issues.parse_handoff(description))


class IsUser(unittest.TestCase):
    def test_case_insensitive_email(self):
        self.assertTrue(issues.is_user(note("x", email="ANN@Example.com"), HUMANS))
        self.assertTrue(issues.is_user(note("x", email="bob@example.com"), HUMANS))

    def test_others(self):
        self.assertFalse(issues.is_user(note("x", email="agent@example.com"), HUMANS))
        self.assertFalse(issues.is_user(note("x", email=None), HUMANS))
        self.assertFalse(issues.is_user(note("x", email=""), HUMANS))


class Brief(unittest.TestCase):
    def test_handoff_instructions(self):
        self.assertEqual(issues.brief(issue(HANDOFF)).splitlines()[0], "Ann, 2026-09-01T00:00:00.000Z:")
        self.assertEqual(issues.brief(issue(f"Handoff from ENG-3: {URL}")), "")

    def test_direct_issue_description(self):
        self.assertEqual(issues.brief(issue("Do the thing.\n\n## Comments\nx")), "Do the thing.\n\n## Comments\nx")


class BuildCutoff(unittest.TestCase):
    def cutoff(self, *notes, created="2026-09-01T00:00:00.000Z"):
        return issues.build_cutoff(issue(created_at=created, notes=notes), HUMANS)

    def test_fallback_created_at(self):
        self.assertEqual(self.cutoff(), "2026-09-01T00:00:00.000Z")
        self.assertEqual(self.cutoff(note("Working on it")), "2026-09-01T00:00:00.000Z")

    def test_latest_non_user_match(self):
        self.assertEqual(self.cutoff(
            note("Build started: a", "2026-09-02T00:00:00.000Z"),
            note("Build started: b", "2026-09-04T00:00:00.000Z"),
            note("Build ready: c", "2026-09-05T00:00:00.000Z")), "2026-09-04T00:00:00.000Z")

    def test_latest_by_time_not_position(self):
        self.assertEqual(self.cutoff(
            note("Build started: b", "2026-09-04T00:00:00.000Z"),
            note("Build started: a", "2026-09-02T00:00:00.000Z")), "2026-09-04T00:00:00.000Z")

    def test_user_notes_ignored(self):
        self.assertEqual(self.cutoff(
            note("Build started: a", "2026-09-02T00:00:00.000Z"),
            note("Build started", "2026-09-03T00:00:00.000Z", email="ANN@example.com")), "2026-09-02T00:00:00.000Z")

    def test_note_without_email_counts_as_non_user(self):
        self.assertEqual(self.cutoff(note("Build started", "2026-09-02T00:00:00.000Z", email=None)),
                         "2026-09-02T00:00:00.000Z")

    def test_body_stripped_before_match(self):
        self.assertEqual(self.cutoff(note("\n  Build started: x\n", "2026-09-02T00:00:00.000Z")),
                         "2026-09-02T00:00:00.000Z")

    def test_word_boundary_and_anchor(self):
        for body in ("Build startedness", "Build starting", "build started", "Re: Build started: x", "Build  started"):
            with self.subTest(body=body):
                self.assertEqual(self.cutoff(note(body, "2026-09-02T00:00:00.000Z")), "2026-09-01T00:00:00.000Z")
        self.assertEqual(self.cutoff(note("Build started", "2026-09-02T00:00:00.000Z")), "2026-09-02T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
