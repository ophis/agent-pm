import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import promote  # noqa: E402

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
STATES = {"Todo": "s-todo", "In Progress": "s-prog", "In Review": "s-review", "Handoff": "s-hand", "Done": "s-done"}
PROJECTS = {"Deep Research": "p-dr", "Product Design": "p-pd", "Engineering": "p-eng"}
HUMAN = {"email": "me@x.com", "name": "Me"}
AGENT = {"email": "agent@x.com", "name": "agent@x.com"}
OTHER = {"email": "other@x.com", "name": "Other"}
CONFIG = """team = "T"
human_members = ["me@x.com"]
[projects."Deep Research"]
next = "Product Design"
[projects."Product Design"]
prefix = "PRD"
"""


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class FakeLinear:
    def __init__(self):
        self.issues, self.children, self.mutations, self.fail = {}, {}, [], set()

    def add(self, ident, project="Deep Research", state="Handoff", **kw):
        self.issues[ident] = dict(id=ident, identifier=ident, url=f"https://l/{ident}", title=f"Title {ident}",
                                  priority=2, createdAt=ago(10000), project={"id": PROJECTS[project], "name": project},
                                  state=state, history=[], comments=[], relations=[], inverse=[], attachments=[])
        self.issues[ident].update(kw)
        return self.issues[ident]

    def moved(self, ident, minutes, to, frm="s-prog"):
        self.issues[ident]["history"].append({"createdAt": ago(minutes), "fromStateId": frm, "toStateId": STATES[to]})

    def said(self, ident, minutes, user=HUMAN, body="build X"):
        self.issues[ident]["comments"].append({"body": body, "createdAt": ago(minutes), "user": user})

    def __call__(self, query, **v):
        if query == promote.Q_SETUP:
            return {"teams": {"nodes": [{"id": "team"}]},
                    "workflowStates": {"nodes": [{"id": i, "name": n} for n, i in STATES.items()]},
                    "projects": {"nodes": [{"id": i, "name": n} for n, i in PROJECTS.items()]}}
        if query == promote.Q_HANDOFF:
            return {"issues": {"nodes": [dict(i, attachments={"nodes": i["attachments"]})
                                         for i in self.issues.values() if i["state"] == "Handoff"]}}
        if query == promote.Q_DETAIL:
            if v["i"] in self.fail:
                raise SystemExit("linear api error: boom")
            i = self.issues[v["i"]]
            # the API returns newest first
            return {"issue": {"history": {"nodes": list(reversed(i["history"]))},
                              "comments": {"nodes": list(reversed(i["comments"]))},
                              "relations": {"nodes": [{"relatedIssue": {"id": r}} for r in i["relations"]]},
                              "inverseRelations": {"nodes": [{"issue": {"id": r}} for r in i["inverse"]]}}}
        if query == promote.Q_CHILD:
            return {"issues": {"nodes": [self.children[v["c"]]] if v["c"] in self.children else []}}
        self.mutations.append((query, v))
        if query == promote.M_CREATE:
            inp = v["in"]
            if "CREATE" in self.fail:
                raise SystemExit("linear api error: create failed")
            child = dict(inp, identifier=f"C-{len(self.children) + 1}")
            self.children[inp["id"]] = child
            return {"issueCreate": {"success": True, "issue": {"id": inp["id"], "identifier": child["identifier"]}}}
        if query == promote.M_RELATE:
            self.issues[v["in"]["issueId"]]["relations"].append(v["in"]["relatedIssueId"])
            return {"issueRelationCreate": {"success": True}}
        if query == promote.M_COMMENT:
            self.issues[v["i"]].setdefault("posted", []).append(v["b"])
            return {"commentCreate": {"success": True}}
        if query == promote.M_STATE:
            self.issues[v["i"]]["state"] = next(n for n, i in STATES.items() if i == v["s"])
            return {"issueUpdate": {"success": True}}
        raise AssertionError(query)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.write_config(CONFIG)
        self.fake = FakeLinear()

    def write_config(self, text):
        path = os.path.join(self.tmp.name, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = promote.main(list(argv), gql=self.fake, now=NOW, config=self.config)
        self.out = out.getvalue()
        return rc

    def ready(self, ident="DR-1", **kw):
        """An issue reviewed (In Review from In Progress 60 min ago), commented, then handed off 30 min ago."""
        issue = self.fake.add(ident, **kw)
        self.fake.moved(ident, 60, "In Review")
        self.fake.said(ident, 45)
        self.fake.moved(ident, 30, "Handoff", frm=STATES["In Review"])
        return issue


class TestPromote(Base):
    def test_happy_path(self):
        src = self.ready(attachments=[{"title": "Report", "url": "https://gh/r.md"}])
        self.assertEqual(self.run_main(), 0)
        (child,) = self.fake.children.values()
        self.assertEqual(child["id"], promote.child_id("DR-1", "p-pd", ago(30)))
        self.assertEqual((child["projectId"], child["stateId"], child["priority"], child["teamId"]),
                         ("p-pd", "s-todo", 2, "team"))
        self.assertEqual(child["title"], "PRD: Title DR-1")
        self.assertEqual(child["description"], "Handoff from DR-1: https://l/DR-1\n\n## Source\n- Report: https://gh/r.md"
                                               f"\n\n## Instructions\nMe, {ago(45)}:\nbuild X")
        self.assertEqual(src["relations"], [child["id"]])
        self.assertEqual(src["state"], "Done")
        self.assertEqual(src["posted"], ["Promoted to C-1."])
        self.assertIn("promote DR-1 -> C-1", self.out)

    def test_no_attachments_omits_source(self):
        self.ready()
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertNotIn("## Source", child["description"])

    def test_comment_filter(self):
        self.fake.add("DR-1")
        self.fake.said("DR-1", 90, body="before cutoff")
        self.fake.moved("DR-1", 60, "In Review")
        self.fake.said("DR-1", 50, user=AGENT, body="agent")
        self.fake.said("DR-1", 49, user=OTHER, body="other")
        self.fake.said("DR-1", 48, user=None, body="bot")
        self.fake.said("DR-1", 47, body="first")
        self.fake.said("DR-1", 46, body="second")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.run_main()
        (child,) = self.fake.children.values()
        instructions = child["description"].split("## Instructions\n")[1]
        self.assertEqual(instructions, f"Me, {ago(47)}:\nfirst\n\nMe, {ago(46)}:\nsecond")

    def test_never_in_review_takes_all_human_comments(self):
        self.fake.add("DR-1")
        self.fake.said("DR-1", 500, body="old")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["Todo"])
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertIn("old", child["description"])

    def test_no_instructions_bounces(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 60, "In Review")
        self.fake.said("DR-1", 90)  # before the cutoff
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.run_main()
        self.assertEqual(self.fake.children, {})
        self.assertEqual(src["state"], "In Review")
        self.assertEqual(src["posted"], [promote.NO_INSTRUCTIONS])
        self.assertIn("handoff-bounce DR-1 no instructions", self.out)

    def test_cutoff_skips_promote_bounce(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 120, "In Review")
        self.fake.said("DR-1", 110, body="old")
        self.fake.moved("DR-1", 100, "Handoff", frm=STATES["In Review"])
        self.fake.moved("DR-1", 90, "In Review", frm=STATES["Handoff"])  # promote's bounce
        self.fake.said("DR-1", 80, body="new")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertIn("old", child["description"])
        self.assertIn("new", child["description"])
        self.assertEqual(child["id"], promote.child_id("DR-1", "p-pd", ago(100)))
        self.assertEqual(src["state"], "Done")


class TestIdempotency(Base):
    def test_existing_child_without_relation(self):
        src = self.ready()
        cid = promote.child_id("DR-1", "p-pd", ago(30))
        self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
        self.run_main()
        self.assertEqual(len(self.fake.children), 1)
        self.assertNotIn(promote.M_CREATE, [q for q, _ in self.fake.mutations])
        self.assertEqual(src["relations"], [cid])
        self.assertEqual(src["state"], "Done")

    def test_existing_child_with_relation(self):
        src = self.ready()
        cid = promote.child_id("DR-1", "p-pd", ago(30))
        self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
        src["inverse"].append(cid)
        self.run_main()
        self.assertNotIn(promote.M_RELATE, [q for q, _ in self.fake.mutations])
        self.assertEqual(src["state"], "Done")

    def test_rerun_after_success_is_noop(self):
        self.ready()
        self.run_main()
        n = len(self.fake.mutations)
        self.run_main()
        self.assertEqual(len(self.fake.mutations), n)  # source is Done, no longer listed

    def test_rehandoff_from_done_reuses_child(self):
        src = self.ready()
        self.run_main()
        self.fake.moved("DR-1", 5, "Handoff", frm=STATES["Done"])
        src["state"] = "Handoff"
        self.run_main()
        self.assertEqual(len(self.fake.children), 1)
        self.assertEqual(src["state"], "Done")

    def test_new_review_cycle_gives_new_child(self):
        src = self.ready()
        self.run_main()
        self.fake.moved("DR-1", 20, "In Review", frm=STATES["In Progress"])
        self.fake.said("DR-1", 15, body="again")
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        src["state"] = "Handoff"
        self.run_main()
        self.assertEqual(len(self.fake.children), 2)

    def test_child_id(self):
        a = promote.child_id("DR-1", "p-pd", "2026-09-27T10:00:00.000Z")
        self.assertEqual(a, promote.child_id("DR-1", "p-pd", "2026-09-27T10:00:00.000Z"))
        self.assertNotEqual(a, promote.child_id("DR-1", "p-pd", "2026-09-27T10:00:01.000Z"))
        self.assertNotEqual(a, promote.child_id("DR-1", "p-eng", "2026-09-27T10:00:00.000Z"))
        self.assertEqual(a[14], "4")  # UUID v4 format


class TestScopeAndConfig(Base):
    def test_project_without_next_untouched(self):
        src = self.ready(project="Product Design")
        self.run_main()
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(src["state"], "Handoff")

    def test_config_only_extension(self):
        self.config = self.write_config(CONFIG + 'next = "Engineering"\n[projects.Engineering]\nprefix = "ENG"\n')
        self.ready(project="Product Design")
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertEqual((child["projectId"], child["title"]), ("p-eng", "ENG: Title DR-1"))

    def test_next_without_prefix_exits(self):
        self.config = self.write_config(CONFIG.replace('prefix = "PRD"\n', ""))
        with self.assertRaises(SystemExit):
            self.run_main()

    def test_next_without_projects_entry_exits(self):
        self.config = self.write_config(CONFIG.replace('next = "Product Design"', 'next = "Nowhere"'))
        with self.assertRaises(SystemExit):
            self.run_main()

    def test_unknown_flag(self):
        self.assertEqual(self.run_main("--now"), 2)


class TestFailures(Base):
    def test_error_on_one_issue_others_still_run(self):
        self.ready("DR-1")
        other = self.ready("DR-2")
        self.fake.fail.add("DR-1")
        self.run_main()
        self.assertEqual(other["state"], "Done")
        self.assertEqual(self.fake.issues["DR-1"]["state"], "Handoff")
        self.assertIn("handoff-error DR-1", self.out)

    def test_failing_over_grace_bounces(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 200, "In Review")
        self.fake.said("DR-1", 190)
        self.fake.moved("DR-1", 90, "Handoff", frm=STATES["In Review"])
        self.fake.fail.add("CREATE")
        self.run_main()
        self.assertEqual(src["state"], "In Review")
        self.assertTrue(src["posted"][0].startswith("Handoff failed: linear api error: create failed"))

    def test_failing_under_grace_waits(self):
        src = self.ready()  # handed off 30 min ago
        self.fake.fail.add("CREATE")
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)

    def test_grace_counts_from_latest_handoff(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 3000, "In Review")
        self.fake.said("DR-1", 2990)
        self.fake.moved("DR-1", 2900, "Handoff", frm=STATES["In Review"])
        self.fake.moved("DR-1", 2800, "In Review", frm=STATES["Handoff"])  # earlier failure bounce
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        self.fake.fail.add("CREATE")
        self.run_main()
        self.assertEqual(src["state"], "Handoff")

    def test_failure_bounce_names_existing_child(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 200, "In Review")
        self.fake.said("DR-1", 190)
        self.fake.moved("DR-1", 90, "Handoff", frm=STATES["In Review"])
        cid = promote.child_id("DR-1", "p-pd", ago(90))
        self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
        orig = self.fake.__call__

        def relate_fails(query, **v):
            if query == promote.M_RELATE:
                raise SystemExit("linear api error: relate failed")
            return orig(query, **v)
        self.fake = relate_fails
        self.run_main()
        self.assertIn("C-9 already exists", src["posted"][0])
        self.assertEqual(src["state"], "In Review")


class TestDryRun(Base):
    def test_no_mutations(self):
        self.ready("DR-1")
        self.fake.add("DR-2")  # no instructions
        self.fake.moved("DR-2", 30, "Handoff", frm=STATES["In Review"])
        self.run_main("--dry-run")
        self.assertEqual(self.fake.mutations, [])
        self.assertIn("dry-run: promote DR-1 -> new Product Design issue", self.out)
        self.assertIn("dry-run: handoff-bounce DR-2", self.out)


if __name__ == "__main__":
    unittest.main()
