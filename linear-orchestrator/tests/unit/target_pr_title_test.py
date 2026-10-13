from agent_pm.linear_orchestrator.engine import target
from orchestrator_test_helpers.unit.target.pr_title_fixtures import PrTitle as PrTitleBase


class PrTitle1(PrTitleBase):
    def test_keeps_unicode_letters_and_digits(self):
        self.assertEqual(
            target.pr_title("TASK-38", "ENG: Engineering 骨架"), "TASK-38: Engineering 骨架"
        )
        self.assertEqual(target.pr_title("TASK-38", "ENG: 中文标题 2"), "TASK-38: 中文标题 2")

    def test_removes_what_single_quotes_cannot_hold(self):
        self.assertEqual(
            target.pr_title(
                "TASK-38", "ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮"
            ),
            "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h",
        )
