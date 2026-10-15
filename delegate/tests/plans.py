"""Base, the plan tests' case, which the detach tests share."""

import io
import os
import re
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace

from agent_pm.delegate import ConfigError, Spec
from agent_pm.delegate.composer import Composer
from agent_pm.delegate.composer.base import TEAM
from agent_pm.delegate.engine.plan import plan

from delegate.tests.case import Case, committed
from delegate.tests.unit.composer.teams import POET, VERSE
from session.tests import fake_adapter
from session.tests.modules import adapter_loader
from utils.tests.case import local_section

SID = "11111111-2222-3333-4444-555555555555"


TASKS = os.path.join(TEAM, "tasks")


TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")


TEAM_FILES = {"roles/poet.md": POET, "tasks/verse.md": VERSE}


def write_team(top, files=TEAM_FILES):
    for rel, text in files.items():
        path = os.path.join(top, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)


class Base(Case):
    """A real temp dir for the workdir `work` (not made), a HOME of the test's own (self.agent_pm)
    and fresh session settings: the core config's adapters and the fake client.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.session_dependencies = {
            "class_loader": adapter_loader({"agent_pm.session.adapters.fake": fake_adapter})
        }
        self.local("")

    def local(self, text):
        """
        The local config `text`, the session configured with its adapters and the fake client.
        """
        local = os.path.join(self.agent_pm, "config.local.toml")
        with open(local, "w") as f:
            f.write(
                local_section(text, "delegate")
                .replace("[delegate.hosts", "[session.hosts")
                .replace("[delegate.adapters", "[session.adapters")
            )
        fake = {"implementation": "agent_pm.session.adapters.fake:FakeAdapter"}
        self.configure_session(committed(local) | {"fake": fake})

    def planned(
        self, role="researcher", task="light-research", *, keys=None, team=None, run=None, **kw
    ):
        """(Plan, stderr) of `run`, else `role` resolved with run keys `keys`: input "Research X.",
        client "fake", headless, out self.out, workdir self.work, the cwd its agent dir, unless `kw`
        says otherwise.
        """
        kw = {"client": "fake", "mode": "headless", "out": self.out, "input": "Research X.", **kw}
        workdir = kw.pop("workdir", self.work)
        composition = Composer(team=team)
        run = run or replace(composition.resolve(role), **(keys or {}))
        err = io.StringIO()
        with redirect_stderr(err):
            got = plan(
                run,
                Spec(kw.pop("input"), workdir, task=task, team=team, **kw),
                driver=self.driver,
                composition=composition,
            )
        return got, err.getvalue()

    def refused(self, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.planned(*args, **kw)
        return str(cm.exception)
