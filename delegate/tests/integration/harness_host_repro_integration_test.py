"""TASK-447 repro, to delete before merge: HarnessHosts' configured fake host case with main's
outcome-only wait, ten times a run, so CI names the late writer.
"""

import unittest

from delegate.tests.integration import harness_host_integration_test as fixed
from session.tests import live_tmux


class OutcomeOnly(fixed.HarnessHosts):
    test_an_injected_fake_host_runs_the_harness_while_tmux_runs_the_agent = None
    test_a_detached_extension_client_reaches_its_outcome = None

    def outcome(self, status: str = "done") -> None:
        def ended():
            self.server.tmux("run-shell", "-b", "true")
            return any(
                line.endswith(f" {fixed.HARNESS} outcome {status}")
                for line in live_tmux.events(self.events)
            )

        live_tmux.wait(ended, fixed.WAIT, what=f"`{fixed.HARNESS} outcome {status}`")


for k in range(10):
    setattr(
        OutcomeOnly,
        f"test_configured_{k}",
        fixed.HarnessHosts.test_a_configured_fake_harness_host_imports_and_routes_the_harness,
    )
OutcomeOnly.test_a_configured_fake_harness_host_imports_and_routes_the_harness = None


if __name__ == "__main__":
    unittest.main()
