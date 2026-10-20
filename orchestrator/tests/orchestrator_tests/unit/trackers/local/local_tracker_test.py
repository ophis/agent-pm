"""LocalTracker: the Tracker read side on a temp board; writes refused."""

import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_pm.orchestrator.engine.flow.handoff import handoff, parse_handoff
from agent_pm.orchestrator.engine.flow.types import (
    Approved,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Pick,
    Recheck,
    Snapshot,
    SubIssue,
)
from agent_pm.orchestrator.engine.types import IssueError, TrackerFileError
from agent_pm.orchestrator.trackers.local.local_tracker import LocalTracker
from orchestrator_tests.case import Case

MTIME = datetime(2026, 10, 20, tzinfo=UTC)
RUN = "Run 0123abcd-0000-4000-8000-0123456789ab · started"


def at(hour, minute=0):
    return datetime(2026, 10, 19, hour, minute, tzinfo=UTC)


def text(ident, *, front="", description="", comments="", history=""):
    body = f"### Description\n{description}\n" if description else ""
    body += f"### Comments\n{comments}" if comments else ""
    body += f"### History\n{history}" if history else ""
    return f"---\nid: {ident}\ntitle: {ident} title\ncreated: 2026-10-19T14:02:00-04:00\n{front}---\n{body}"


def checked(role, assignee, task, client, *, ready):
    return Recheck(ready, role, None, assignee, None, task, client)


class Board(Case):
    def setUp(self):
        zone = mock.patch.dict(os.environ, {"TZ": "UTC0"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.mkdir(self.board)
        self.tracker = LocalTracker(self.board, ("pm", "engineer"))

    def write(self, rel, ident, **parts):
        path = os.path.join(self.board, rel, f"{ident} x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(text(ident, **parts))
        os.utime(path, (MTIME.timestamp(), MTIME.timestamp()))
        return path


class Listing(Board):
    def test_assigned_lists_role_dirs_only(self):
        path = self.write("pm/ready", "LOC-1", front="priority: 2\n")
        self.write("engineer/ready", "LOC-2")
        self.write("pm/running", "LOC-3")
        for rel, ident in (
            ("unassigned/ready", "LOC-4"),
            ("me/ready", "LOC-5"),
            ("stranger/ready", "LOC-6"),
            ("stranger/running", "LOC-7"),
            ("me/running", "LOC-8"),
            ("pm/review", "LOC-9"),
            (".state/pm/ready", "LOC-11"),
        ):
            self.write(rel, ident)
        self.write("archive", "LOC-10", history="- 2026-10-19 14:02 ready self\n")
        ready = self.tracker.assigned(IssueState.READY)
        self.assertEqual([r["identifier"] for r in ready], ["LOC-2", "LOC-1"])
        self.assertEqual(
            ready[1],
            {
                "id": "LOC-1",
                "identifier": "LOC-1",
                "url": Path(path).as_uri(),
                "title": "LOC-1 title",
                "priority": 2,
                "createdAt": "2026-10-19T18:02:00.000Z",
                "assignee": {"id": "pm"},
                "project": None,
            },
        )
        self.assertEqual(self.tracker.role(ready[1]), "pm")
        running = self.tracker.assigned(IssueState.RUNNING)
        self.assertEqual([r["identifier"] for r in running], ["LOC-3"])
        with self.assertRaises(ValueError):
            self.tracker.assigned(IssueState.REVIEW)

    def test_ready_blockers(self):
        blockers = "blocked_by: [LOC-2, LOC-3, LOC-4, LOC-5, LOC-6, LOC-7, LOC-8, "
        blockers += "LOC-9, LOC-10, LOC-11, LOC-12, LOC-99]\n"
        self.write("pm/ready", "LOC-1", front=blockers)
        self.write("pm/backlog", "LOC-2")
        self.write("engineer/ready", "LOC-3")
        self.write("pm/running", "LOC-4")
        self.write("pm/review", "LOC-5")
        self.write("pm/approved", "LOC-6")
        self.write("pm/done", "LOC-7")
        self.write("me/canceled", "LOC-8")
        self.write("archive", "LOC-9", history="- 2026-10-19 14:02 done self\n")
        self.write("archive", "LOC-10", history="- 2026-10-19 14:02 review self\n")
        self.write("archive", "LOC-11")
        self.write(
            "archive",
            "LOC-12",
            history="- 2026-10-19 14:02 review self\n- 2026-10-19 15:02 canceled other\n",
        )
        got = {r["identifier"]: found for r, found in self.tracker.ready()}
        self.assertEqual(
            got,
            {
                "LOC-3": (),
                "LOC-1": (
                    "LOC-2",
                    "LOC-3",
                    "LOC-4",
                    "LOC-5",
                    "LOC-6",
                    "LOC-10",
                    "LOC-11",
                    "LOC-99",
                ),
            },
        )
        self.assertEqual([r["identifier"] for r, _ in self.tracker.ready()], ["LOC-3", "LOC-1"])

    def test_scans_once(self):
        self.write("pm/ready", "LOC-1")
        self.assertEqual(len(self.tracker.assigned(IssueState.READY)), 1)
        self.write("pm/ready", "LOC-2")
        self.assertEqual(len(self.tracker.assigned(IssueState.READY)), 1)
        self.assertEqual(len(LocalTracker(self.board, ("pm",)).ready()), 2)

    def test_scan_errors_raise_on_first_read(self):
        shutil.rmtree(self.board)
        tracker = LocalTracker(self.board, ("pm",))
        with self.assertRaises(TrackerFileError) as caught:
            tracker.ready()
        self.assertEqual(caught.exception.reason, "no such board directory")


class Recheck_(Board):
    def test_recheck_picks(self):
        path = self.write("pm/ready", "LOC-1", front="task: research\nclient: c1\n")
        self.write("pm/ready", "LOC-2")
        self.tracker.ready()
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked("pm", {"id": "pm"}, Pick("research"), Pick("c1"), ready=True),
        )
        self.assertEqual(
            self.tracker.recheck("LOC-2"),
            checked("pm", {"id": "pm"}, Pick(), Pick(), ready=True),
        )
        with open(path, "w", encoding="utf-8") as file:
            file.write(text("LOC-1", front="task: build\n"))
        self.assertEqual(self.tracker.recheck("LOC-1").task, Pick("build"))
        os.makedirs(os.path.join(self.board, "pm/running"))
        moved = os.path.join(self.board, "pm/running/LOC-1.md")
        os.rename(path, moved)
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked("pm", {"id": "pm"}, Pick("build"), Pick(), ready=False),
        )
        os.makedirs(os.path.join(self.board, "me/ready"))
        os.rename(moved, os.path.join(self.board, "me/ready/LOC-1.md"))
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked(None, {"id": "me"}, Pick("build"), Pick(), ready=False),
        )
        os.remove(os.path.join(self.board, "me/ready/LOC-1.md"))
        self.assertFalse(self.tracker.recheck("LOC-1").ready)
        self.assertFalse(self.tracker.recheck("LOC-404").ready)


class Moves(Board):
    def test_moved_at_by_people_derived_move(self):
        history = "- 2026-10-19 14:02 ready other\n- 2026-10-19 14:30 running self\n"
        self.write("pm/ready", "LOC-1", history=history + "- 2026-10-19 16:10 review self\n")
        self.write(
            "pm/ready",
            "LOC-2",
            history="- 2026-10-19 14:02 ready other\n" + "- 2026-10-19 15:00 ready self\n",
        )
        self.write("pm/running", "LOC-3")
        moved = self.tracker.moved_at
        self.assertEqual(moved("LOC-1", IssueState.READY, by_people=True), MTIME)
        self.assertEqual(moved("LOC-1", IssueState.RUNNING), at(14, 30))
        self.assertIsNone(moved("LOC-1", IssueState.RUNNING, by_people=True))
        self.assertEqual(moved("LOC-2", IssueState.READY, by_people=True), at(14, 2))
        self.assertEqual(moved("LOC-2", IssueState.READY), at(15))
        self.assertEqual(moved("LOC-3", IssueState.RUNNING, by_people=True), MTIME)
        self.assertIsNone(moved("LOC-3", IssueState.READY))
        self.assertIsNone(moved("LOC-404", IssueState.READY))

    def test_detail_moves_and_comments(self):
        comments = (
            "\n#### other · 2026-10-19 14:05\nlook at LOC-7\n\n"
            f"#### self · 2026-10-19 14:31 <!-- id=c1 key=run:1 -->\n{RUN}\n\n"
            "#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nPRD ready\n\n"
            "###\napproved, over to engineer\n"
        )
        history = (
            "- 2026-10-19 14:02 ready other\n- 2026-10-19 14:30 running self\n"
            "- 2026-10-19 16:10 review self\n"
        )
        front = "related: [LOC-7]\nblocked_by: [LOC-10]\n"
        self.write("pm/approved", "LOC-1", front=front, comments=comments, history=history)
        self.write("engineer/ready", "LOC-20", front="related: [LOC-1]\n")
        self.write("archive", "LOC-21", front="blocked_by: [LOC-1]\n")
        self.write("pm/ready", "LOC-22", front="related: [LOC-2]\n")
        got = self.tracker.detail("LOC-1")
        self.assertEqual(
            got,
            Detail(
                IssueState.APPROVED,
                (
                    Move("2026-10-19T14:02:00.000Z", IssueState.READY, None),
                    Move("2026-10-19T14:30:00.000Z", IssueState.RUNNING, IssueState.READY),
                    Move("2026-10-19T16:10:00.000Z", IssueState.REVIEW, IssueState.RUNNING),
                    Move("2026-10-20T00:00:00.000Z", IssueState.APPROVED, IssueState.REVIEW),
                ),
                (
                    Comment(
                        "2026-10-19T14:05:00.000Z", "look at LOC-7", "other", other=True, run=False
                    ),
                    Comment("2026-10-19T14:31:00.000Z", RUN, "self", other=False, run=True),
                    Comment(
                        "2026-10-19T16:10:00.000Z", "PRD ready", "self", other=False, run=False
                    ),
                    Comment(
                        "2026-10-20T00:00:00.000Z",
                        "approved, over to engineer",
                        "other",
                        other=True,
                        run=False,
                    ),
                ),
                frozenset({"LOC-7", "LOC-10", "LOC-20", "LOC-21"}),
            ),
        )
        self.write("pm/backlog", "LOC-30")
        self.assertEqual(
            LocalTracker(self.board, ("pm",)).detail("LOC-30").moves,
            (Move("2026-10-20T00:00:00.000Z", None, None),),
        )
        self.assertIsNone(self.tracker.detail("LOC-20").moves[0].frm)
        with self.assertRaises(IssueError):
            self.tracker.detail("LOC-404")

    def test_read(self):
        self.write("pm/review", "LOC-1", history="- 2026-10-19 16:10 review self\n")
        self.write("me/backlog", "LOC-2")
        self.write("archive", "LOC-3", history="- 2026-10-19 16:10 done self\n")
        self.write("archive", "LOC-4")
        self.write("unassigned/canceled", "LOC-5", history="- 2026-10-19 16:10 ready self\n")
        read = self.tracker.read
        self.assertEqual(read("LOC-1"), Snapshot(IssueState.REVIEW, at(16, 10)))
        self.assertEqual(read("LOC-2"), Snapshot(None, MTIME))
        self.assertEqual(read("LOC-3"), Snapshot(IssueState.DONE, at(16, 10)))
        self.assertEqual(read("LOC-4"), Snapshot(None, None))
        self.assertEqual(read("LOC-5"), Snapshot(IssueState.CANCELED, MTIME))
        self.assertIsNone(read("LOC-404"))


class Reads(Board):
    def test_issue(self):
        description = handoff("LOC-9", "https://x/9", sources=[], instructions=[], comments=[])
        comments = (
            "\n#### self · 2026-10-19 14:31\n" + RUN + "\n\n"
            "#### self · 2026-10-19 16:10\nPRD ready\n\n"
            "#### other · 2026-10-19 15:00\nlooks good\n"
        )
        front = "related: [LOC-2, LOC-99]\nblocked_by: [LOC-3]\nlinks:\n"
        front += '  - title: "the doc"\n    url: https://x/doc\n'
        path = self.write(
            "pm/ready", "LOC-1", front=front, description=description, comments=comments
        )
        self.write("pm/review", "LOC-2")
        self.write("archive", "LOC-3", history="- 2026-10-19 16:10 done self\n")
        self.write("archive", "LOC-5")
        self.write("engineer/ready", "LOC-4", front="related: [LOC-1, LOC-5]\n")
        got = self.tracker.issue("LOC-1")
        self.assertEqual(
            (got.id, got.identifier, got.url, got.title, got.description),
            ("LOC-1", "LOC-1", Path(path).as_uri(), "LOC-1 title", description),
        )
        self.assertEqual(
            (got.created_at, got.home, got.labels), ("2026-10-19T18:02:00.000Z", None, ())
        )
        self.assertEqual(
            got.notes,
            (
                Note("looks good", "2026-10-19T15:00:00.000Z", None, "other"),
                Note("PRD ready", "2026-10-19T16:10:00.000Z", None, "self"),
            ),
        )
        self.assertEqual(got.links, (Link("https://x/doc"),))
        self.assertEqual(
            got.linked,
            (
                Linked("LOC-2", "LOC-2 title", "review"),
                Linked("LOC-3", "LOC-3 title", "done"),
                Linked("LOC-4", "LOC-4 title", "ready"),
            ),
        )
        self.assertIsNotNone(got.handoff)
        self.assertEqual(got.handoff, parse_handoff(description))
        self.assertEqual(
            self.tracker.issue("LOC-5").linked, (Linked("LOC-4", "LOC-4 title", "ready"),)
        )
        self.assertEqual(
            self.tracker.issue("LOC-4").linked[1], Linked("LOC-5", "LOC-5 title", "archived")
        )
        with self.assertRaises(IssueError):
            self.tracker.issue("LOC-404")

    def test_search(self):
        canceled = "\n#### other · 2026-10-19 16:00\nlast\n\n#### other · 2026-10-19 15:00\nfirst\n"
        self.write(
            "pm/ready",
            "LOC-1",
            description="Fix the PARSER bug",
            front="links:\n  - title: d\n    url: u1\n",
        )
        self.write(
            "archive",
            "LOC-2",
            description="parser bug",
            comments=canceled,
            history="- 2026-10-19 16:10 canceled self\n",
        )
        self.write("pm/review", "LOC-3", description="nothing", comments=canceled)
        self.write("me/backlog", "LOC-4", description="parser")
        got = self.tracker.search(["parser", "Bug"], ["LOC-3"])
        self.assertEqual(
            got,
            (
                Found("LOC-3", "LOC-3 title", "review", Group.IN_REVIEW, (), "nothing", ""),
                Found("LOC-2", "LOC-2 title", "canceled", Group.CANCELED, (), "parser bug", "last"),
                Found(
                    "LOC-1", "LOC-1 title", "ready", Group.TODO, ("u1",), "Fix the PARSER bug", ""
                ),
            ),
        )
        self.assertEqual(self.tracker.search([], []), ())
        self.assertEqual(
            [(f.identifier, f.group) for f in self.tracker.search(["title"], [])],
            [
                ("LOC-2", Group.CANCELED),
                ("LOC-4", Group.BACKLOG),
                ("LOC-1", Group.TODO),
                ("LOC-3", Group.IN_REVIEW),
            ],
        )

    def test_search_caps_matches_at_ten(self):
        for n in range(1, 13):
            self.write("pm/ready", f"LOC-{n}")
        self.assertEqual(len(self.tracker.search(["title"], [])), 10)
        self.assertEqual(len(self.tracker.search(["title"], ["LOC-1", "LOC-2"])), 12)

    def test_search_groups(self):
        for n, rel in enumerate(("pm/running", "pm/approved", "pm/done", "pm/backlog"), 1):
            self.write(rel, f"LOC-{n}")
        self.write("archive", "LOC-5")
        got = {f.identifier: (f.state, f.group) for f in self.tracker.search(["title"], [])}
        self.assertEqual(
            got,
            {
                "LOC-1": ("running", Group.IN_PROGRESS),
                "LOC-2": ("approved", Group.IN_PROGRESS),
                "LOC-3": ("done", Group.DONE),
                "LOC-4": ("backlog", Group.BACKLOG),
                "LOC-5": ("archived", Group.BACKLOG),
            },
        )

    def test_description_links_on_board(self):
        self.write(
            "pm/ready",
            "LOC-1",
            description="the text",
            front="links:\n  - title: a\n    url: u1\n  - title: b\n    url: u2\n",
        )
        self.write("archive", "LOC-2")
        self.assertEqual(self.tracker.description("LOC-1"), "the text")
        self.assertEqual(self.tracker.description("LOC-2"), "")
        self.assertIsNone(self.tracker.description("LOC-404"))
        self.assertEqual(self.tracker.links("LOC-1"), {"u1", "u2"})
        self.assertEqual(self.tracker.on_board("LOC-2"), "LOC-2")
        self.assertIsNone(self.tracker.on_board("LOC-404"))

    def test_sub_issues(self):
        self.write("pm/ready", "LOC-1")
        self.write("pm/done", "LOC-2", front="parent: LOC-1\n")
        self.write("engineer/ready", "LOC-3", front="parent: LOC-1\n")
        self.write("archive", "LOC-4", front="parent: LOC-1\n")
        self.write("me/canceled", "LOC-5", front="parent: LOC-1\n")
        self.write("pm/ready", "LOC-6", front="parent: LOC-3\n")
        self.assertEqual(
            self.tracker.sub_issues("LOC-1"),
            (
                SubIssue("LOC-3", "LOC-3", finished=False),
                SubIssue("LOC-5", "LOC-5", finished=True),
                SubIssue("LOC-2", "LOC-2", finished=True),
            ),
        )

    def test_finished(self):
        self.write("pm/done", "LOC-1", history="- 2026-10-19 16:10 done self\n")
        self.write("pm/ready", "LOC-3", front="parent: LOC-1\n")
        self.write("engineer/done", "LOC-2")
        self.write("engineer/canceled", "LOC-4")
        for rel, ident in (
            ("me/done", "LOC-5"),
            ("me/canceled", "LOC-6"),
            ("stranger/canceled", "LOC-7"),
            ("unassigned/canceled", "LOC-8"),
        ):
            self.write(rel, ident)
        self.write("archive", "LOC-9", history="- 2026-10-19 16:10 done self\n")
        self.assertEqual(
            self.tracker.finished({"pm"}),
            [
                Finished("LOC-4", "LOC-4", MTIME, ()),
                Finished(
                    "LOC-1", "LOC-1", at(16, 10), (SubIssue("LOC-3", "LOC-3", finished=False),)
                ),
            ],
        )

    def test_approved(self):
        front = "priority: 2\nlinks:\n  - title: the doc\n    url: https://x/doc\n"
        path = self.write("pm/approved", "LOC-1", front=front)
        self.write("engineer/approved", "LOC-2")
        self.write("me/approved", "LOC-3")
        self.write("archive", "LOC-4", history="- 2026-10-19 16:10 approved self\n")
        self.assertEqual(
            self.tracker.approved({"pm"}),
            [
                Approved(
                    "LOC-1",
                    "LOC-1",
                    Path(path).as_uri(),
                    "LOC-1 title",
                    2,
                    "2026-10-19T14:02:00-04:00",
                    "",
                    "",
                    "pm",
                    (("the doc", "https://x/doc"),),
                )
            ],
        )
        self.assertEqual(
            [a.owner for a in self.tracker.approved({"pm", "engineer"})], ["engineer", "pm"]
        )

    def test_find_comment(self):
        comments = (
            "\n#### other · 2026-10-19 14:05 <!-- id=c0 key=result:ab12 -->\nnot mine\n\n"
            "#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nPRD ready\n"
        )
        self.write("pm/review", "LOC-1", comments=comments)
        self.assertEqual(self.tracker.find_comment("LOC-1", ("result", "ab12")), "PRD ready")
        self.assertIsNone(self.tracker.find_comment("LOC-1", ("result", "zz")))


class Writes(Board):
    def test_writes_not_supported(self):
        t = self.tracker
        calls = {
            "created": lambda: t.created(("a",)),
            "create": lambda: t.create(
                ("a",), home="", owner="pm", priority=0, title="t", description=""
            ),
            "relate": lambda: t.relate("LOC-1", "LOC-2"),
            "comment": lambda: t.comment("LOC-1", "b"),
            "move": lambda: t.move("LOC-1", IssueState.DONE),
            "claim": lambda: t.claim("LOC-1"),
            "comment_and_move": lambda: t.comment_and_move(
                "LOC-1", "b", to=IssueState.REVIEW, frm=IssueState.RUNNING, notify=False
            ),
            "notify": lambda: t.notify("LOC-1", ()),
            "link": lambda: t.link("LOC-1", "u", "t"),
            "retitle": lambda: t.retitle("LOC-1", "t"),
            "archive": lambda: t.archive("LOC-1"),
            "unarchive": lambda: t.unarchive("LOC-1"),
            "put_comment": lambda: t.put_comment("LOC-1", "b", ("a",)),
        }
        for name, call in calls.items():
            with self.subTest(name), self.assertRaises(NotImplementedError) as caught:
                call()
            self.assertEqual(str(caught.exception), f"local board: {name} is not supported yet")
        self.assertEqual(os.listdir(self.board), [])
