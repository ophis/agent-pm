"""modes/tui.py with its hook run for real, over a fake tmux module: a hook firing while close
removes a session's files recreates none of them.
"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_pm.session.engine.types import Spec
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes import tui

from session.tests import fake_adapter
from session.tests.case import Case
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.tui_support import SID, fake_host

NAME = "w1"


class CloseTest(Case):
    def test_close_prevents_a_late_hook_from_recreating_files_during_removal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, host = os.path.join(self.agent_pm, "tui"), fake_host()
        tui.TuiMode.start(
            Spec(
                workdir=tmp.name,
                name=NAME,
                runtime="fake",
                sid=SID,
                events=os.path.join(tmp.name, "events"),
            ),
            fake_adapter.FakeAdapter({"tui": True, "scenario": "", "log": ""}),
            registry=self.driver.registry,
            record=lambda _info: None,
            host=host,
        )
        host.status.return_value = tmux_host.PaneStatus.RUNNING
        remove, hooks = os.rmdir, []

        def remove_after_hook(path, *args, **kwargs):
            hooks.append(
                subprocess.run(
                    [*tui._Store(NAME).hook_argv(PACKAGE_ROOTS), "stop"],
                    input=b"{}",
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            )
            return remove(path, *args, **kwargs)

        with mock.patch.object(os, "rmdir", remove_after_hook):
            tui.TuiMode.close(NAME, host=host)
        self.assertEqual(len(hooks), 1)
        self.assertIn(b"session.json", hooks[0].stderr)
        self.assertFalse(os.path.lexists(os.path.join(root, NAME)))
        self.assertEqual(os.listdir(root), [f"{NAME}.lock"])


if __name__ == "__main__":
    unittest.main()
