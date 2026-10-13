from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.run_fixtures import logged
from orchestrator_test_helpers.unit.writeback.data import ID, TARGET, TREE, expect, outcome
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback.url_check_fixtures import UrlCheck as UrlCheckBase
from orchestrator_test_helpers.unit.writeback_support import PR


class UrlCheck1(UrlCheckBase):
    def test_an_engineering_url_must_be_on_the_target(self):
        evil = "https://github.com/ophis/agent-pm-evil/pull/1"
        for name, target, o, calls, skip in (
            (
                "other repo dropped",
                TARGET,
                outcome("done", "ENG-7: x", "Ready.", url=evil),
                expect(body="Build ready: Ready.", state="in_review"),
                f"skip {ID} reason=url not on ophis/agent-pm",
            ),
            (
                "case-insensitive match",
                ("Ophis", "Agent-PM"),
                outcome("done", "ENG-7: x", "Ready.", url=PR),
                expect(
                    body="Build ready: Ready.",
                    state="in_review",
                    attach=PR,
                    attach_title="ENG-7: x",
                ),
                None,
            ),
            (
                "no target drops every url",
                None,
                outcome("failed", "ENG-7: x", "push not permitted", url=TREE),
                expect(body="Build failed: push not permitted", state="in_review"),
                f"skip {ID} reason=url not on a target repo",
            ),
        ):
            with self.subTest(name):
                gql = Gql()
                before = len(logged())
                writeback.finish(self.ctx(gql=gql, target=target), o)
                self.assertEqual(gql.calls, calls)
                self.assertEqual(
                    [line for line in self.lines()[before:] if line.startswith("skip ")],
                    [skip] if skip else [],
                )
