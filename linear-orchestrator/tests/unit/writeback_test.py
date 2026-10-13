from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.data import APPROVE
from orchestrator_test_helpers.unit.writeback.say_and_approve_fixtures import (
    SayAndApprove as SayAndApproveBase,
)
from orchestrator_test_helpers.unit.writeback_support import APPROVE_MAPPED


class SayAndApprove1(SayAndApproveBase):
    def test_say(self):
        self.assertEqual(writeback.say("", "just text"), "just text")
        self.assertEqual(writeback.say("Build started:", ""), "Build started")
        self.assertEqual(
            writeback.say("Build started:", "first build of the PRD"),
            "Build started: first build of the PRD",
        )
        self.assertEqual(writeback.say("Build ready:", "a\nb"), "Build ready:\na\nb")

    def test_approve_line(self):
        self.assertEqual(writeback.approve_line(None), APPROVE)
        self.assertEqual(writeback.approve_line("ophis/agent-pm"), APPROVE_MAPPED)

    def test_queries_verbatim(self):
        self.assertEqual(
            writeback.M_TITLE,
            "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }",
        )
        self.assertEqual(
            writeback.M_ATTACH,
            "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }",
        )
        self.assertEqual(
            writeback.M_UNARCHIVE, "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
        )
        self.assertEqual(
            writeback.Q_ATTACHED,
            "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }",
        )
        self.assertEqual(writeback.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }")
