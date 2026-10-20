"""Local issue file parsing: the TASK-778 format and each refusal's reason."""

import os
import time
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from agent_pm.orchestrator.engine.types import TrackerFileError
from agent_pm.orchestrator.trackers.local import issue_file
from agent_pm.orchestrator.trackers.local.types import (
    Entry,
    FileLink,
    LocalState,
    Note,
    Source,
)
from orchestrator_tests.case import Case

PATH = "/board/pm/ready/LOC-12 x.md"
MTIME = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FRONT = "---\nid: LOC-12\ntitle: x\ncreated: 2026-10-19T14:02:00-04:00\n---\n"
SAMPLE = """---
id: LOC-12
title: "PRD: 抽象 orchestrator 与驱动引擎"
task:
client: claude
priority: 2
created: 2026-10-19T14:02:00-04:00
blocked_by: [LOC-10]
related: [LOC-7]
parent: LOC-5
links:
  - title: 抽象 orchestrator 与驱动引擎
    url: https://github.com/.../x.md
---

### Description
Repo: ophis/agent-pm
……

### Comments

#### other · 2026-10-19 14:05
先看 LOC-7 的盘点。

#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->
PRD ready: https://github.com/.../x.md

###
通过，交给 engineer。

### History
- 2026-10-19 14:02 ready other
- 2026-10-19 14:30 running self
- 2026-10-19 16:10 review self
"""


def at(hour, minute):
    """2026-10-19 hour:minute in the tests' zone, UTC-3."""
    return datetime(2026, 10, 19, hour, minute, tzinfo=timezone(timedelta(hours=-3)))


def parse(text, *, assignee="pm", state=LocalState.READY):
    return issue_file.parse(PATH, text, assignee=assignee, state=state, mtime=MTIME)


class Parse(Case):
    def setUp(self):
        zone = mock.patch.dict(os.environ, {"TZ": "XXX+03"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()

    def refuses(self, text, reason):
        with self.assertRaises(TrackerFileError) as caught:
            parse(text)
        self.assertEqual((caught.exception.path, caught.exception.reason), (PATH, reason))

    def test_sample(self):
        got = parse(SAMPLE)
        self.assertEqual(
            (got.path, got.assignee, got.state, got.mtime, got.id, got.title),
            (PATH, "pm", LocalState.READY, MTIME, "LOC-12", "PRD: 抽象 orchestrator 与驱动引擎"),
        )
        self.assertEqual((got.task, got.client, got.priority), (None, "claude", 2))
        self.assertEqual(got.created, datetime(2026, 10, 19, 18, 2, tzinfo=UTC))
        self.assertEqual(
            (got.blocked_by, got.related, got.parent), (("LOC-10",), ("LOC-7",), "LOC-5")
        )
        self.assertEqual(
            got.links, (FileLink("抽象 orchestrator 与驱动引擎", "https://github.com/.../x.md"),)
        )
        self.assertEqual(got.description, "Repo: ophis/agent-pm\n……")
        self.assertEqual(
            got.comments,
            (
                Note(at(14, 5), Source.OTHER, "先看 LOC-7 的盘点。", None),
                Note(
                    at(16, 10), Source.SELF, "PRD ready: https://github.com/.../x.md", "result:ab12"
                ),
                Note(MTIME, Source.OTHER, "通过，交给 engineer。", None),
            ),
        )
        self.assertEqual(
            got.history,
            (
                Entry(at(14, 2), LocalState.READY, Source.OTHER),
                Entry(at(14, 30), LocalState.RUNNING, Source.SELF),
                Entry(at(16, 10), LocalState.REVIEW, Source.SELF),
            ),
        )

    def test_defaults(self):
        got = parse(FRONT)
        self.assertEqual(
            (
                got.task,
                got.client,
                got.priority,
                got.blocked_by,
                got.related,
                got.parent,
                got.links,
            ),
            (None, None, 0, (), (), None, ()),
        )
        self.assertEqual((got.description, got.comments, got.history), ("", (), ()))

    def test_scalars_and_lists(self):
        text = (
            "---\nid: LOC-1\ntitle: x\ncreated: 2026-10-19T14:02:00Z\ntask: build\n"
            'client: " x "\nblocked_by: []\nrelated: [A-1, B2-30]\n---\n'
        )
        got = parse(text)
        self.assertEqual(
            (got.task, got.client, got.blocked_by, got.related),
            ("build", " x ", (), ("A-1", "B2-30")),
        )
        self.assertEqual(got.created, datetime(2026, 10, 19, 14, 2, tzinfo=UTC))

    def test_bare_comment_is_other_at_mtime(self):
        got = parse(FRONT + "\n### Comments\n\n###\nlooks fine\n\nship it\n")
        self.assertEqual(got.comments, (Note(MTIME, Source.OTHER, "looks fine\n\nship it", None),))

    def test_empty_json_string_is_none(self):
        got = parse(FRONT[:-4] + 'task: ""\nclient: ""\nparent: ""\n---\n')
        self.assertEqual((got.task, got.client, got.parent), (None, None, None))

    def test_heading_in_comment_body(self):
        got = parse(FRONT + "### Comments\n###\nplan:\n#### Steps\n1. read\n")
        self.assertEqual(
            got.comments, (Note(MTIME, Source.OTHER, "plan:\n#### Steps\n1. read", None),)
        )

    def test_bad_header_after_a_comment(self):
        self.refuses(
            FRONT + "### Comments\n###\nhi\n#### other · 2026-13-19 14:05\nyo\n",
            "bad comment header '#### other · 2026-13-19 14:05'",
        )

    def test_hidden_id_and_key(self):
        got = parse(
            FRONT
            + "### Comments\n#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nok\n"
        )
        self.assertEqual(got.comments, (Note(at(16, 10), Source.SELF, "ok", "result:ab12"),))

    def test_history_sorted_stable_in_local_zone(self):
        got = parse(
            FRONT
            + "### History\n- 2026-10-19 16:10 review self\n\n- 2026-10-19 14:02 ready other\n"
            + "- 2026-10-19 16:10 approved other\n"
        )
        self.assertEqual(
            got.history,
            (
                Entry(at(14, 2), LocalState.READY, Source.OTHER),
                Entry(at(16, 10), LocalState.REVIEW, Source.SELF),
                Entry(at(16, 10), LocalState.APPROVED, Source.OTHER),
            ),
        )
        self.assertEqual(got.history[0].at, datetime(2026, 10, 19, 17, 2, tzinfo=UTC))

    def test_archived_state_is_last_history_state(self):
        history = "### History\n- 2026-10-19 16:10 done self\n- 2026-10-19 14:02 ready other\n"
        self.assertEqual(parse(FRONT + history, assignee=None, state=None).state, LocalState.DONE)
        self.assertIsNone(parse(FRONT, assignee=None, state=None).state)

    def test_no_front_matter(self):
        self.refuses("id: LOC-1\n", "no front matter")

    def test_front_matter_not_closed(self):
        self.refuses("---\nid: LOC-1\n", "front matter has no closing '---'")

    def test_bad_front_matter_line(self):
        self.refuses(FRONT.replace("title: x", "Title = x"), "bad front-matter line 'Title = x'")

    def test_unknown_key(self):
        self.refuses(FRONT.replace("title: x", "title: x\nstatus: ready"), "unknown key 'status'")

    def test_duplicate_key(self):
        self.refuses(FRONT.replace("title: x", "title: x\ntitle: y"), "duplicate key 'title'")

    def test_missing_id(self):
        self.refuses(FRONT.replace("id: LOC-12\n", ""), "missing id")

    def test_missing_title(self):
        self.refuses(FRONT.replace("title: x", "title:"), "missing title")

    def test_missing_created(self):
        self.refuses(FRONT.replace("created: 2026-10-19T14:02:00-04:00\n", ""), "missing created")

    def test_bad_id(self):
        self.refuses(FRONT.replace("LOC-12", "loc-12"), "bad id 'loc-12'")
        self.refuses(FRONT.replace("LOC-12", "LOC-12/x"), "bad id 'LOC-12/x'")

    def test_bad_priority(self):
        self.refuses(FRONT.replace("title: x", "title: x\npriority: 5"), "bad priority '5'")

    def test_bad_created(self):
        self.refuses(
            FRONT.replace("2026-10-19T14:02:00-04:00", "2026-10-19 14:02"),
            "bad created '2026-10-19 14:02': want ISO 8601 with an offset or Z",
        )
        self.refuses(
            FRONT.replace("2026-10-19T14:02:00-04:00", "soon"),
            "bad created 'soon': want ISO 8601 with an offset or Z",
        )

    def test_bad_string(self):
        self.refuses(FRONT.replace("title: x", 'title: "x'), "bad string '\"x'")

    def test_bad_list(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nrelated: LOC-1"), "bad list related: 'LOC-1'"
        )

    def test_bad_list_item(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nblocked_by: [LOC-1, ../x]"),
            "bad blocked_by id '../x'",
        )

    def test_bad_parent(self):
        self.refuses(FRONT.replace("title: x", "title: x\nparent: LOC 5"), "bad parent 'LOC 5'")

    def test_bad_links_line(self):
        self.refuses(
            FRONT.replace("title: x", "title: x\nlinks:\n  - url: u"), "bad links line '  - url: u'"
        )
        self.refuses(
            FRONT.replace("title: x", "title: x\nlinks:\n  - title: t\n    url:"),
            "bad links line '    url:'",
        )

    def test_text_before_first_section(self):
        self.refuses(FRONT + "hello\n### Description\n", "text before the first section: 'hello'")

    def test_section_order(self):
        self.refuses(FRONT + "### History\n### Comments\n", "section 'Comments' after 'History'")

    def test_duplicate_section(self):
        self.refuses(
            FRONT + "### Description\n### Description\n", "duplicate section 'Description'"
        )

    def test_bad_comment_header(self):
        self.refuses(
            FRONT + "### Comments\n#### someone · 2026-10-19 14:05\nhi\n",
            "bad comment header '#### someone · 2026-10-19 14:05'",
        )
        self.refuses(
            FRONT + "### Comments\n#### other · 2026-13-19 14:05\nhi\n",
            "bad comment header '#### other · 2026-13-19 14:05'",
        )

    def test_text_before_first_comment(self):
        self.refuses(FRONT + "### Comments\nhi\n", "bad comment header 'hi'")

    def test_bad_history_line(self):
        self.refuses(
            FRONT + "### History\n- 2026-10-19 14:02 todo other\n",
            "bad History line '- 2026-10-19 14:02 todo other'",
        )
