"""issue_refs: a value resolves to the one entity it names in scope, else IssueError with the
candidates and the lookup command.
"""

from agent_pm.linear_orchestrator.engine import issue_refs
from agent_pm.linear_orchestrator.engine.types import GraphQLErrors, IssueError, LinearError
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.issue_fixtures import OTHER_TEAM, Routes, edges, project, team, uid

MISSING = GraphQLErrors([{"message": "Entity not found: Team"}])


class Team(Case):
    def test_a_key_or_exact_name_matches_one_team(self):
        gql = Routes(teams=edges([team(1, "TASK")]))
        self.assertEqual(issue_refs.team(gql, "task")["key"], "TASK")
        want = {"or": [{"name": {"eq": "task"}}, {"key": {"eqIgnoreCase": "task"}}]}
        self.assertEqual(gql.sent("teams")[0]["f"], want)

    def test_an_id_is_read_by_id(self):
        gql = Routes(team=team(7, "ENG"))
        self.assertEqual(issue_refs.team(gql, uid(7))["key"], "ENG")
        self.assertEqual(gql.sent("team"), [{"id": uid(7)}])

    def test_an_unknown_id_names_the_lookup(self):
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(Routes(team=MISSING), uid(7))
        self.assertIn("no such team", str(cm.exception))
        self.assertIn("agent-pm-linear issue lookup teams", str(cm.exception))
        self.assertNotIn("--query", str(cm.exception))

    def test_other_failures_are_not_a_missing_entity(self):
        with self.assertRaises(LinearError):
            issue_refs.team(Routes(team=GraphQLErrors([{"message": "rate limited"}])), uid(7))

    def test_several_matches_are_rejected_with_every_candidate(self):
        # the second match is on the next page: the first is never taken alone
        gql = Routes(
            teams=[
                edges([team(1, "ENG", "Core")], more=True, cursor="c1"),
                edges([team(2, "CORE", "Platform")]),
            ]
        )
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(gql, "core")
        text = str(cm.exception)
        for found in (uid(1), uid(2), "2 match", "lookup teams --query core"):
            self.assertIn(found, text)

    def test_no_match_lists_near_candidates_and_the_lookup(self):
        gql = Routes(teams=[edges([]), edges([team(3, "COR", "Corvid")], more=True)])
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(gql, "Cor eng")
        text = str(cm.exception)
        self.assertIn(f"candidates: {uid(3)} COR 'Corvid', …", text)
        self.assertIn("lookup teams --query 'Cor eng'", text)
        near = gql.sent("teams")[1]
        self.assertEqual(near["first"], issue_refs.CANDIDATES)
        self.assertEqual(near["f"]["or"][1], {"key": {"containsIgnoreCase": "Cor eng"}})

    def test_no_candidates_says_none(self):
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(Routes(teams=edges([])), "zzz")
        self.assertIn("candidates: none", str(cm.exception))


class Project(Case):
    def test_an_exact_name_matches_within_the_team(self):
        gql = Routes(team={"projects": edges([project(4, "Agent PM")])})
        self.assertEqual(issue_refs.project(gql, TEAM, "Agent PM")["id"], uid(4))
        sent = gql.sent("team")[0]
        self.assertEqual((sent["t"], sent["f"]), (TEAM, {"name": {"eq": "Agent PM"}}))

    def test_an_id_must_belong_to_the_team(self):
        gql = Routes(project=project(4, "Agent PM"))
        self.assertEqual(issue_refs.project(gql, TEAM, uid(4))["id"], uid(4))
        self.assertEqual(gql.sent("project"), [{"id": uid(4), "t": TEAM}])
        outside = Routes(project=project(5, "Elsewhere", team_ids=()))
        with self.assertRaises(IssueError) as cm:
            issue_refs.project(outside, OTHER_TEAM, uid(5))
        text = str(cm.exception)
        self.assertIn(f"is not a project of team {OTHER_TEAM}", text)
        self.assertIn(f"lookup projects --team {OTHER_TEAM}", text)

    def test_same_named_projects_are_rejected(self):
        gql = Routes(team={"projects": edges([project(4, "Ops"), project(5, "Ops")])})
        with self.assertRaises(IssueError) as cm:
            issue_refs.project(gql, TEAM, "Ops")
        self.assertIn(f"{uid(4)} 'Ops', {uid(5)} 'Ops'", str(cm.exception))


class Filters(Case):
    def test_every_skips_empty_filters(self):
        a, b = {"name": {"eq": "a"}}, {"key": {"eq": "b"}}
        self.assertIsNone(issue_refs.every(None, None))
        self.assertEqual(issue_refs.every(a, None), a)
        self.assertEqual(issue_refs.every(a, b), {"and": [a, b]})

    def test_text_filters(self):
        self.assertIsNone(issue_refs.named(None))
        self.assertIsNone(issue_refs.team_text(""))
        self.assertIsNone(issue_refs.user_text(None))
        self.assertEqual(
            issue_refs.user_text("ann"),
            {
                "or": [
                    {"name": {"containsIgnoreCase": "ann"}},
                    {"email": {"containsIgnoreCase": "ann"}},
                ]
            },
        )
