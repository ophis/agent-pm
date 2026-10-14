"""Base, the plan tests' case, which the detach tests share."""

import io
import os
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace

from agent_pm.delegate import ConfigError, Spec, composer, plan

from delegate.tests.case import Case, committed
from session.tests import fake_adapter
from session.tests.modules import adapter_loader

SID = "11111111-2222-3333-4444-555555555555"


class Base(Case):
    """A real temp dir for the workdir `work` (not made), a HOME of the test's own (self.agent_pm)
    and fresh session settings: the core config's adapters and the fake runtime.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.driver_dependencies = {
            "class_loader": adapter_loader({"agent_pm.session.adapters.fake": fake_adapter})
        }
        self.local("")

    def local(self, text):
        """
        The local config `text`, the session configured with its adapters and the fake runtime.
        """
        local = os.path.join(self.agent_pm, "config.local.toml")
        with open(local, "w") as f:
            f.write(text)
        fake = {"implementation": "agent_pm.session.adapters.fake:FakeAdapter"}
        self.configure_session(committed(local) | {"fake": fake})

    def planned(
        self, role="researcher", task="light-research", *, keys=None, team=None, run=None, **kw
    ):
        """(Plan, stderr) of `run`, else `role` resolved with run keys `keys`: input "Research X.",
        runtime "fake", headless, out self.out, workdir self.work, the cwd the workdir, unless `kw`
        says otherwise.
        """
        kw = {"runtime": "fake", "mode": "headless", "out": self.out, "input": "Research X.", **kw}
        workdir = kw.pop("workdir", self.work)
        kw.setdefault("cwd", workdir)
        run = run or replace(composer.resolve(role, team=team), **(keys or {}))
        err = io.StringIO()
        with redirect_stderr(err):
            got = plan(
                run, Spec(kw.pop("input"), workdir, task=task, team=team, **kw), driver=self.driver
            )
        return got, err.getvalue()

    def refused(self, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.planned(*args, **kw)
        return str(cm.exception)
