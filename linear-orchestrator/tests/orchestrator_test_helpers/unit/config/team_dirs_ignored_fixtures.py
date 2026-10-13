import os

from agent_pm import delegate
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.config.config_file_fixtures import ConfigFile
from orchestrator_test_helpers.unit.config.data import PIPELINE, SID, STEPS, write


class TeamDirsIgnored(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.team = os.path.realpath(os.path.join(self.agent_pm, "team"))
        writer = (
            "# Writer\n\nYou write.\n\n## Tasks\n\nPick the task below that fits the input; unsure → `one-pager` "
            "(`<tasks>/one-pager.md`).\n\n- `one-pager` (`<tasks>/one-pager.md`): a brief; always; light.\n"
        )
        for rel, text in (
            ("tasks/one-pager.md", "# One-Pager\n" + STEPS),
            ("roles/writer.md", writer),
        ):
            path = os.path.join(self.team, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write(path, text)
        self.work = os.path.join(os.path.dirname(self.agent_pm), "work")

    def seen(self):
        pm = self.load(PIPELINE).roles["pm"]
        return pm, delegate.plan(
            pm.run,
            delegate.Spec(
                "x",
                self.work,
                runtime="claude",
                sid=SID,
                out=os.path.join(self.work, "out.md"),
                cwd=self.work,
            ),
        )
