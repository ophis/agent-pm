"""A worker's view through bin/agent-pm-session on a private tmux server (TASK-471): `start
--display none` opens no pane, the caller still its opener, and is hidden; show opens it and clears
hidden; restart and resume keep hidden or shown; a new start of the same sid takes its own view;
its events still reach the events file; a given hidden part is refused; a Codex worker the same.
"""

import os
import shutil
import tempfile

from agent_pm.utils import Record

from session.tests import live_tmux
from session.tests.case import REPO
from session.tests.cli_support import MANAGER, CliCase
from session.tests.integration.clients.codex import fake_tui

WINDOW = f"={MANAGER}:"


class HiddenCase(CliCase):
    def count(self) -> int:
        return len(self.server.panes(WINDOW))

    def hidden(self, name: str = "w1", **kw) -> bool:
        (found,) = self.status(name, **kw)
        return found.hidden

    def viewer(self, name: str = "w1") -> str:
        """Its @pane, "" when show opened none for this tmux session."""
        return self.server.option(name, "@pane")

    def shows(self, name: str = "w1") -> str:
        """Its @pane, once that pane in the manager's window runs a client of it."""
        pane = self.viewer(name)
        self.assertIn(pane, self.server.panes(WINDOW))
        tty = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_tty}").stdout.strip()

        def attached():
            found = self.server.tmux("list-clients", "-t", f"={name}", "-F", "#{client_tty}")
            return tty in found.stdout.split()

        live_tmux.wait(attached, what=f"{pane} to attach {name}")
        return pane

    def again(self, cmd: str, name: str = "w1", **kw):
        return self.session(cmd, name, **self.scene(name), **kw)

    def kill_session(self, name: str = "w1") -> None:
        self.ok(self.server.tmux("kill-session", "-t", f"={name}"))


class HiddenStartTest(HiddenCase):
    def test_display_none_starts_without_a_pane_hidden_and_opened_by_the_caller(self):
        before = self.count()
        record = self.record(self.start("w1", "--display", "none"))
        self.assertEqual(record.display, f"{MANAGER}///hidden")
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.assertTrue(self.hidden())
        listed = self.ok(self.session("ls", "--here", "--records")).stdout.splitlines()
        self.assertIn(record, [Record.parse(line) for line in listed])

    def test_show_opens_the_hidden_worker_and_clears_hidden(self):
        self.record(self.start("w1", "--display", "none"))
        self.ok(self.session("show", "w1"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_a_shown_display_none_worker_stays_shown_after_restart(self):
        self.record(self.start("w1", "--display", "none"))
        self.wait_event("w1", "done")
        self.ok(self.session("show", "w1"))
        self.shows()
        self.record(self.again("restart"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_a_hidden_worker_stays_hidden_after_restart_and_resume(self):
        self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.shows()
        self.ok(self.session("hide", "w1"))
        before = self.count()
        self.record(self.again("restart"))
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertTrue(self.hidden())
        self.kill_session()
        self.record(self.again("resume"))
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertTrue(self.hidden())

    def test_a_new_start_of_the_same_sid_takes_its_own_view(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.ok(self.session("hide", "w1"))
        self.ok(self.session("close", "w1"))
        self.record(self.start("w1", "--resume", record.sid))
        self.shows()
        self.assertFalse(self.hidden())
        self.ok(self.session("close", "w1"))
        self.record(self.start("w1", "--resume", record.sid, "--display", "none"))
        self.assertEqual(self.viewer(), "")
        self.assertTrue(self.hidden())

    def test_a_hidden_workers_events_reach_the_events_file_without_a_pane(self):
        before = self.count()
        self.record(self.start("w1", "--display", "none"))
        self.wait_event("w1", "done")
        self.assertEqual(self.count(), before)
        self.assertTrue(self.hidden())

    def test_a_default_start_opens_a_pane_not_hidden(self):
        self.record(self.start("w1"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_start_refuses_a_given_hidden_part(self):
        res = self.start("w1", "--display", f"{MANAGER}///hidden")
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("its hidden part comes only from none", res.stderr)
        self.assertFalse(self.server.tmux("has-session", "-t", "=w1").returncode == 0)


class CodexHiddenTest(HiddenCase):
    def setUp(self):
        super().setUp()
        # Codex may write /tmp and $TMPDIR, where the fixture's HOME lies; its state lies elsewhere
        root = tempfile.mkdtemp(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(self.server.kill)
        home, bin_dir = os.path.join(root, "home"), os.path.join(root, "bin")
        for path in (home, bin_dir):
            os.mkdir(path)
        shutil.copy2(self.server.program, bin_dir)
        fake_tui.install(bin_dir)
        scenario = os.path.join(root, "w1.codex.json")
        with open(scenario, "w", encoding="utf-8") as f:
            f.write('{"tui_turns": [["Hi from w1."]]}')
        self.extra = {
            "HOME": home,
            "PATH": bin_dir + os.pathsep + self.env()["PATH"],
            "FAKE_SCENARIO": scenario,
            "FAKE_LOG": os.path.join(root, "w1.codex.jsonl"),
        }

    def codex(self, cmd: str, *args: str):
        if cmd != "start":
            return self.session(cmd, "w1", *args, **self.extra)
        argv = ("start", "w1", "--client", "codex", "--cwd", self.cwd, "--prompt", "hi", *args)
        return self.session(*argv, AGENT_PM_EVENTS=self.events, **self.extra)

    def test_a_codex_worker_hides_shows_and_restarts_like_claude(self):
        before = self.count()
        record = self.record(self.codex("start", "--display", "none"))
        self.assertEqual((record.detail, record.display), ("codex", f"{MANAGER}///hidden"))
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertTrue(self.hidden(**self.extra))
        self.ok(self.codex("show"))
        self.shows()
        self.assertFalse(self.hidden(**self.extra))
        self.record(self.codex("restart"))
        self.shows()
        self.assertFalse(self.hidden(**self.extra))
        self.ok(self.codex("hide"))
        self.assertEqual(self.count(), before)
        self.assertTrue(self.hidden(**self.extra))
