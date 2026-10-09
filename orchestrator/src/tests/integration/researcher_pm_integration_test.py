"""The researcher → pm segment as steps (segments.Segment): a Backlog research issue the router skips, the human's
move to Todo, a claimed agent run, write-back, the human's handoff and promote's pm issue."""
import os
import sys
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
from segments import Segment  # noqa: E402


class ResearcherToPm(Segment):
    def test_a1_research_to_a_pm_issue(self):
        """A1: researcher_to_pm (its claim step checks the claim event's task is null); the child is a pm start."""
        self.assert_pm_start(self.researcher_to_pm())


if __name__ == "__main__":
    unittest.main()
