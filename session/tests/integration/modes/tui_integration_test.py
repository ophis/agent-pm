"""Tui cleanup retires hook paths and closes arbitrary Host processes."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.session.engine.types import Spec
from agent_pm.session.hosts.tmux import TmuxHost
from agent_pm.session.hosts.tmux.host import Tmux
from agent_pm.session.modes import tui

from session.tests import fake_adapter
from session.tests.case import Case
from session.tests.fakes.terminal import ScreenHost
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.tui_support import SID, Live

NAME = "w1"


class CloseTest(Case):
    def test_close_prevents_a_late_hook_from_recreating_files_during_removal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, host = os.path.join(self.agent_pm, "tui"), ScreenHost()
        tui.TuiRunner.start(
            Spec(
                workdir=tmp.name,
                name=NAME,
                runtime="fake",
                sid=SID,
                events=os.path.join(self.home, "events"),
            ),
            fake_adapter.FakeAdapter({"tui": True, "scenario": "", "log": ""}),
            registry=self.driver.registry,
            record=lambda _info: None,
            host=host,
        )
        remove, hooks = os.rmdir, []

        def remove_after_hook(path, *args, **kwargs):
            hooks.append(
                subprocess.run(
                    [*tui._Store(NAME, host=host).hook_argv(PACKAGE_ROOTS), "stop"],
                    input=b"{}",
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            )
            return remove(path, *args, **kwargs)

        with mock.patch.object(os, "rmdir", remove_after_hook):
            tui.TuiRunner.close(NAME, host=host)
        self.assertEqual(len(hooks), 1)
        self.assertIn(b"session.json", hooks[0].stderr)
        self.assertFalse(os.path.lexists(os.path.join(root, NAME)))
        self.assertEqual(os.listdir(root), [f"{NAME}.lock"])


class RawSessionCloseTest(Live):
    def test_legacy_close_removes_an_untagged_session_and_its_mode_state(self):
        name = "untagged"
        created = self.server.tmux("new-session", "-d", "-s", name, "cat", "-")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(self.server.option(name, "@kind"), "")
        folder = Path(self.agent_pm, "tui", name)
        folder.mkdir(parents=True)
        (folder / "session.json").write_text("{}")
        self.driver.close(name)
        self.assertNotEqual(self.server.tmux("has-session", "-t", f"={name}").returncode, 0)
        self.assertFalse(folder.exists())
        self.driver.close(name)


class PollCostTest(Live):
    def test_a_quiet_poll_of_a_live_terminal_runs_five_tmux_commands(self):
        commands = []

        def counted(argv, **kwargs):
            commands.append(argv[1])
            return subprocess.run(argv, **kwargs)

        host = TmuxHost("tmux", tmux=Tmux(proc=counted))
        scenario = self.config["adapters"]["fake"]["scenario"]
        os.makedirs(self.workdir)
        with open(scenario, "w") as f:
            json.dump([{"read": True}], f)
        mode = tui.TuiRunner.start(
            Spec(
                workdir=self.workdir,
                name=NAME,
                runtime="fake",
                sid=SID,
                prompt="hello",
                events=self.events,
            ),
            self.driver.registry.adapter("fake"),
            registry=self.driver.registry,
            record=lambda _info: None,
            host=host,
        )
        self.addCleanup(tui.TuiRunner.close, NAME, host=host)
        while mode.poll(0)[0]:
            pass
        commands.clear()
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            commands,
            [
                "display-message",
                "display-message",
                "display-message",
                "capture-pane",
                "display-message",
            ],
        )


if __name__ == "__main__":
    unittest.main()
