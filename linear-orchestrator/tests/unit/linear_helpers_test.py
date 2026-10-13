import re

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.helpers_fixtures import Helpers as HelpersBase


class Helpers1(HelpersBase):
    def test_issue_id_is_unanchored(self):
        self.assertEqual(re.findall(linear.ISSUE_ID, "see ENG-12, PM-3."), ["ENG-12", "PM-3"])
        for bad in ("task-7", "7-ENG", "ENG-", "ENG-7 "):
            self.assertIsNone(re.fullmatch(linear.ISSUE_ID, bad), bad)

    def test_one_line(self):
        self.assertEqual(linear.one_line("a\n\n  b\tc "), "a b c")
        self.assertEqual(linear.one_line(RuntimeError("two\nlines")), "RuntimeError: two lines")
        self.assertEqual(linear.one_line(LinearError("api error")), "LinearError: api error")

    def test_stamp_is_local_time_in_seconds(self):
        self.assertRegex(linear.stamp(), r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\Z")
