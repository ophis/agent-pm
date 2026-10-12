"""orchestrator/com.ophis.agent-pm.{router,promote}.plist, the launchd jobs: each runs its entry from this tree's
import roots under the deploy checkout, its output into the orchestrator's log."""

import os
import plistlib

from new.tests.case import NEW, ORCH_ROOT, ROOT, Case

REPO = os.path.dirname(NEW)
DEPLOY = "/Users/francis/playground/agent-pm"
LOG = "/Users/francis/.agent-pm/logs/orchestrator.jsonl"
JOBS = ("router", "promote")


def plist(job):
    with open(os.path.join(REPO, "orchestrator", f"com.ophis.agent-pm.{job}.plist"), "rb") as f:
        return plistlib.load(f)


class Plists(Case):
    def test_the_plists_send_launchd_output_to_the_log(self):
        for job in JOBS:
            with self.subTest(job=job):
                job_plist = plist(job)
                self.assertEqual(
                    {job_plist["StandardOutPath"], job_plist["StandardErrorPath"]}, {LOG}
                )

    def test_each_runs_its_entry_with_both_new_roots_on_pythonpath(self):
        roots = [os.path.join(DEPLOY, os.path.relpath(root, REPO)) for root in (ROOT, ORCH_ROOT)]
        for job in JOBS:
            with self.subTest(job=job):
                job_plist = plist(job)
                module = f"agent_pm_linear.{job}"
                self.assertEqual(
                    job_plist["ProgramArguments"][:4],
                    ["/opt/homebrew/bin/python3", "-P", "-m", module],
                )
                self.assertEqual(job_plist["EnvironmentVariables"]["PYTHONPATH"].split(":"), roots)
                self.assertTrue(os.path.isfile(os.path.join(ORCH_ROOT, *module.split(".")) + ".py"))
