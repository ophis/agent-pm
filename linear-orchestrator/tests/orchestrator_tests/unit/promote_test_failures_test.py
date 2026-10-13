import functools
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, promote
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.promote_support import (
    CONFIG,
    NOW,
    ROLE,
    STATES,
    Base,
    FakeLinear,
    FakePruner,
    ago,
    instructions,
)
from orchestrator_tests.unit.run_fixtures import HARNESS, logged, show


class TestFailures(Base):
    def test_error_on_one_issue_others_still_run(self):
        self.ready("DR-1")
        other = self.ready("DR-2")
        self.fake.fail[promote.Q_DETAIL, "DR-1"] = "boom"
        self.run_main()
        self.assertEqual(other["state"], "Done")
        self.assertEqual(self.fake.issues["DR-1"]["state"], "Handoff")
        self.assertIn("handoff-error DR-1", self.out)

    def test_failing_over_grace_bounces(self):
        for fail, existing, posted in (
            (promote.M_CREATE, False, "Handoff failed: linear api error: create failed"),
            (
                promote.M_RELATE,
                True,
                "Handoff failed: linear api error: relate failed The "
                "next-stage issue C-9 already exists.",
            ),
        ):
            with self.subTest(existing=existing):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                if existing:
                    cid = promote.child_id("DR-1", "pm", ago(90))
                    self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
                self.fake.fail[fail] = (
                    "create failed" if fail == promote.M_CREATE else "relate failed"
                )
                self.run_main()
                self.assertEqual(
                    (src["state"], src["posted"], src["subscribers"]),
                    ("In Review", [posted], ["me@x.com"]),
                )

    def test_grace_counts_from_latest_handoff(self):
        src = self.ready(handoff=2900)
        self.fake.moved("DR-1", 2800, "In Review", frm=STATES["Handoff"])
        self.fake.said("DR-1", 20, body="again")
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        self.fake.fail[promote.M_CREATE] = "create failed"
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)
        self.fake.fail.clear()
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertEqual(
            instructions(child["description"]),
            f"## Instructions\nMe, {ago(2915)}:\nbuild X\n\nMe, {ago(20)}:\nagain",
        )
        self.assertEqual(child["id"], promote.child_id("DR-1", "pm", ago(2900)))
        self.assertEqual(src["state"], "Done")


class TestPartialFailures(Base):
    def moved_after_detail(self, state):
        fake = self.fake

        def gql(query, **v):
            out = fake(query, **v)
            if query == promote.Q_DETAIL:
                fake.issues[v["i"]]["state"] = state
            return out

        gql.writes = fake.writes
        self.fake = gql

    def test_comment_failure_after_done_keeps_done(self):
        src = self.ready(handoff=90)
        self.fake.fail[linear.M_COMMENT] = "comment failed"
        self.run_main()
        self.assertEqual(src["state"], "Done")
        self.assertEqual(
            self.out.splitlines()[-1],
            "handoff-error DR-1 reason=promoted, but the comment failed "
            "error=LinearError: linear api error: comment failed",
        )

    def test_failed_bounce_posts_no_comment_and_stays_in_handoff(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.fake.fail[linear.M_STATE] = "boom"
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)
        self.assertIn("handoff-error DR-1", self.out)

    def test_subscribe_failure_noted_in_the_bounce(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.fake.fail[linear.M_SUBSCRIBE] = "boom"
        self.run_main()
        self.assertEqual(
            (src["state"], src["posted"]),
            (
                "In Review",
                [
                    promote.NO_INSTRUCTIONS
                    + "\n\nCould not subscribe me@x.com: LinearError: linear api error: boom"
                ],
            ),
        )
        self.assertIn("handoff-bounce DR-1 to=in_review reason=no instructions", self.out)

    def test_skipped_done_move_posts_no_comment(self):
        src = self.ready()
        self.moved_after_detail("In Review")
        self.run_main()
        self.assertEqual(src["state"], "In Review")
        self.assertNotIn("posted", src)
        self.assertEqual(
            self.fake.writes(), [promote.M_CREATE, promote.M_RELATE, linear.Q_ISSUE_STATE]
        )
        self.assertEqual(self.out, f"move-skip DR-1 step=promote state={STATES['In Review']}")

    def test_skipped_bounce_posts_no_comment(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.moved_after_detail("Todo")
        self.run_main()
        self.assertEqual((src["state"], src["subscribers"]), ("Todo", ["me@x.com"]))
        self.assertNotIn("posted", src)
        self.assertEqual(self.fake.writes(), [linear.M_SUBSCRIBE, linear.Q_ISSUE_STATE])
        self.assertEqual(self.out, f"move-skip DR-1 step=handoff-bounce state={STATES['Todo']}")


class TestDryRun(Base):
    def test_no_mutations(self):
        self.ready("DR-1")
        self.fake.add("DR-2")
        self.fake.moved("DR-2", 30, "Handoff", frm=STATES["In Review"])
        self.run_main("--dry-run")
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(
            self.out.splitlines(),
            [
                "promote DR-1 role=pm project=Deep Research",
                "handoff-bounce DR-2 to=in_review reason=no instructions",
            ],
        )


class TestConfigError(Base):
    def test_a_config_error_is_logged_once_a_day_printed_only_on_a_terminal(self):
        from orchestrator_tests.unit.streams import Stderr

        FakePruner.calls.clear()
        cases = (
            ("bogus = 1\n" + CONFIG, "linear-orchestrator/config.toml: unknown keys: bogus"),
            (
                CONFIG + role_table("bogus"),
                "linear-orchestrator/config.toml: role 'bogus' is not in delegate/config.toml",
            ),
        )
        for text, reason in cases:
            with self.subTest(reason=reason):
                self.write_config(text)
                before = len(logged(self.logs))
                for tty, printed in ((False, ""), (True, f"promote.py: {reason}\n")):
                    err, out = Stderr(tty), io.StringIO()
                    with redirect_stderr(err), redirect_stdout(out):
                        rc = promote.main(
                            [],
                            gql=self.fake,
                            now=NOW,
                            config_path=self.config_path,
                            pruner=FakePruner,
                        )
                    self.assertEqual((rc, err.getvalue(), out.getvalue()), (1, printed, ""))
                events = logged(self.logs)[before:]
                self.assertEqual(
                    [(e["src"], show(e)) for e in events],
                    [("promote", f"config-error reason={reason}")],
                )
        self.assertEqual((self.fake.calls, FakePruner.calls), ([], []))

    def test_a_bad_work_dir_is_said_on_stderr_and_logs_nothing(self):
        self.write_local('work_dir = "w"\n')
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(
            self.err,
            "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n",
        )
        self.assertEqual((self.fake.calls, FakePruner.calls), ([], []))
        self.assertFalse(os.path.exists(self.logs))


class Outage(Base):
    def test_a_failure_at_setup_is_one_linear_error_a_day(self):
        src = self.ready()
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                for said in (f"linear-error op=teams {FIELDS[name]}", ""):
                    gql = failing(error)
                    self.assertEqual(self.run_main(gql=gql), 1)
                    self.assertEqual((self.out, self.err, FakePruner.calls), (said, "", []))
                    self.assertEqual(gql.failed, [(linear.Q_TEAM, {"service": HARNESS, "t": TEAM})])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.run_main(), src["state"]), (0, "Done"))

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.assertEqual(self.run_main("--dry-run", gql=failing(error)), 1)
                self.assertEqual(
                    (self.out, len(self.err.splitlines()), FakePruner.calls),
                    (f"linear-error op=teams {FIELDS[name]}", 1, []),
                )
        self.assertFalse(os.path.exists(self.logs))

    def test_a_failure_on_a_later_issue_stops_the_run(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = fake = FakeLinear()
                first, later = self.ready("DR-1", handoff=90), self.ready("DR-2", handoff=90)
                down = failing(error, gql=fake, ops={"issue"})
                self.assertEqual(
                    self.run_main(
                        gql=lambda q, **v: (down if v.get("i") == "DR-2" else fake)(q, **v)
                    ),
                    1,
                )
                self.assertEqual(
                    (self.out, self.err, FakePruner.calls),
                    (f"linear-error op=issue {FIELDS[name]}", "", []),
                )
                self.assertIn((promote.Q_DETAIL, {"i": "DR-1"}), fake.calls)
                self.assertEqual(
                    down.failed, [(promote.Q_DETAIL, {"service": HARNESS, "i": "DR-2"})]
                )
                self.assertEqual(
                    (first["state"], later["state"], fake.mutations), ("Handoff", "Handoff", [])
                )

    def test_a_failure_mid_promote_past_grace_bounces_nothing_and_the_next_run_promotes_once(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                gql = failing(error, gql=self.fake, ops={"issueRelationCreate"})
                self.assertEqual(self.run_main(gql=gql), 1)
                self.assertEqual(
                    (self.out, self.err, FakePruner.calls),
                    (f"linear-error op=issueRelationCreate {FIELDS[name]}", "", []),
                )
                self.assertEqual(
                    (src["state"], src["relations"], "posted" in src), ("Handoff", [], False)
                )
                self.assertEqual((self.run_main(), self.out), (0, "promote DR-1 to=C-1"))
                (child,) = self.fake.children.values()
                self.assertEqual(
                    (src["state"], src["relations"], src["posted"]),
                    ("Done", [child["id"]], ["Promoted to C-1."]),
                )
                self.assertEqual(
                    [q for q, _ in self.fake.mutations],
                    [promote.M_CREATE, promote.M_RELATE, linear.M_STATE, linear.M_COMMENT],
                )

    def test_a_failure_bouncing_a_failed_handoff_stops_the_run(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                self.fake.fail[promote.M_CREATE] = "create failed"
                self.assertEqual(
                    self.run_main(gql=failing(error, gql=self.fake, ops={"issueUpdate"})), 1
                )
                self.assertEqual(
                    self.out.splitlines(),
                    [
                        "handoff-error DR-1 error=LinearError: linear api error: create failed",
                        f"linear-error op=issueUpdate {FIELDS[name]}",
                    ],
                )
                self.assertEqual(
                    (src["state"], "posted" in src, self.err, FakePruner.calls),
                    ("Handoff", False, "", []),
                )


class TestPath(Base):
    def test_main_sets_path_first(self):
        seen = []

        def spy(query, **v):
            seen.append(os.environ["PATH"])
            return self.fake(query, **v)

        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.run_main("--dry-run", gql=spy)
            self.assertEqual(os.environ["PATH"], config.path())
        self.assertEqual(set(seen), {config.path()})
        self.assertIn("/opt/homebrew/bin", config.path())


class TestPruneHook(Base):
    def test_prune_runs_each_tick(self):
        self.run_main("--dry-run")
        self.assertEqual(len(FakePruner.calls), 1)
        gql, now, dry, work, team, roles = FakePruner.calls[0]
        self.assertIsInstance(gql, functools.partial)
        self.assertEqual((gql.func, gql.keywords), (self.fake, {"service": HARNESS}))
        self.assertEqual((now, dry, work), (NOW, True, os.path.join(self.work, "work")))
        self.assertEqual(team, linear.Team(TEAM, "Team", dict(IDS_BY_KEY)))
        self.assertEqual(roles, {f"u-{role}": role for role in ROLE})

    def test_prune_error_does_not_break_promote(self):
        class Boom:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                raise RuntimeError("boom")

        self.ready("DR-1")
        for name, pruner in (("run error", Boom),):
            with self.subTest(name):
                rc = self.run_main("--dry-run", pruner=pruner)
                self.assertEqual(rc, 0)
                self.assertEqual(
                    self.out.splitlines()[0], "promote DR-1 role=pm project=Deep Research"
                )
                self.assertRegex(
                    self.out.splitlines()[1],
                    r"^prune-error error=RuntimeError: boom",
                )

