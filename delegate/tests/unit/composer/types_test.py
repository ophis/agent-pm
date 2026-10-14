"""composer/types.py: RunConfig's effort is the session layer's Effort."""

from agent_pm.delegate.composer import ConfigError, RunConfig
from agent_pm.session import Effort

from delegate.tests.case import Case


def run_config(effort) -> RunConfig:
    return RunConfig(role="writer", tier=2, effort=effort, runtime="a", output={"type": "local"})


class RunConfigEffort(Case):
    def test_each_effort_and_its_value_is_accepted(self):
        for effort in (*Effort, "xhigh"):
            with self.subTest(effort=effort):
                self.assertIs(run_config(effort).effort, Effort(effort))

    def test_another_value_is_refused_naming_each_effort(self):
        for bad, shown in (("ultra", "'ultra'"), ("LOW", "'LOW'"), (3, "3"), (["low"], "['low']")):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                run_config(bad)
            self.assertEqual(
                str(cm.exception),
                f"effort must be one of low, medium, high, xhigh, max, got {shown}",
            )
