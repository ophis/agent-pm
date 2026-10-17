"""Codex detached owners retain complete fixture installations through callback and recovery."""

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from agent_pm.utils import Record, Status

from delegate.tests.integration.cli import ASKING, DONE, SID
from delegate.tests.replies import block, mark
from session.tests import live_tmux
from session.tests.installation import UpgradeCase


class HarnessUpgrade(UpgradeCase):
    def setUp(self):
        super().setUp()
        self.callback = self.root / "writeback.jsonl"
        for installation in (self.a, self.b):
            command = installation / "bin/fixture-writeback"
            command.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                "from agent_pm.delegate.event import DelegateEvent, outcome\n"
                "event = DelegateEvent.parse(sys.stdin.read())\n"
                "json.loads(pathlib.Path(outcome.SCHEMA).read_text())\n"
                "with open(sys.argv[1], 'a') as stream:\n"
                "    stream.write(json.dumps({'kind': event.kind, 'sid': event.sid, 'schema': outcome.SCHEMA}) + '\\n')\n"
            )
            command.chmod(0o755)
        self.saved = self.work / "saved.txt"
        self.saved.write_text("saved work")

    def delegate(self, installation, *args):
        result = subprocess.run(
            (str(installation / "bin/agent-pm-delegate"), *map(str, args)),
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def start_run(self, outcome):
        self.scenario.write_text(
            json.dumps(
                {
                    "tui_turns": [
                        [
                            mark("start", "running"),
                            {"kind": "pause", "file": str(self.gate)},
                            block(outcome),
                        ]
                    ]
                }
            )
        )
        return Record.parse(
            self.delegate(
                self.a,
                "run",
                "--client",
                "codex",
                "--role",
                "dummy-tester",
                "--task",
                "echo",
                "--mode",
                "tui",
                "--display",
                "none",
                "--input",
                "saved request",
                "--out",
                self.work / "out.md",
                "--workdir",
                self.work,
                "--sid",
                SID,
                "--events",
                self.root / "events",
                "--detach",
                "--on-event",
                shlex.join((str(self.a / "bin/fixture-writeback"), str(self.callback))),
            ).stdout
        )

    def delivered(self):
        return (
            [json.loads(line) for line in self.callback.read_text().splitlines()]
            if self.callback.exists()
            else []
        )

    def finished(self, count):
        return len([row for row in self.delivered() if row["kind"] == "finished"]) >= count

    def test_harness_finishes_writeback_after_A_disappears_but_terminal_keeps_its_copy(self):
        self.start_run(DONE)
        live_tmux.wait(
            lambda: any(row.get("hook") == "UserPromptSubmit" for row in self.hooks()),
            what="run startup",
        )
        self.assertEqual(len(list(self.snapshots.iterdir())), 2)
        shutil.rmtree(self.a)
        self.gate.touch()
        live_tmux.wait(lambda: self.finished(1), timeout=30, what="preserved write-back")
        live_tmux.wait(
            lambda: len(list(self.snapshots.iterdir())) == 1,
            timeout=30,
            what="harness-only cleanup",
        )
        self.assertEqual((self.work / "out.md").read_text(), DONE["deliverable"])
        self.assertEqual({row["sid"] for row in self.delivered()}, {SID})
        self.assertTrue(all(str(self.snapshots) in row["schema"] for row in self.delivered()))
        self.assertEqual({row["exit"] for row in self.hooks()}, {0})
        self.delegate(self.b, "close", self.work)
        live_tmux.wait(lambda: not list(self.snapshots.iterdir()), what="terminal cleanup")

    def test_legacy_pre_fix_metadata_resumes_on_B_without_losing_work_or_history(self):
        record = self.start_run(ASKING)
        self.gate.touch()
        live_tmux.wait(lambda: self.finished(1), timeout=30, what="first outcome write-back")
        live_tmux.wait(
            lambda: not Status.parse(self.delegate(self.b, "status", self.work).stdout).alive,
            timeout=30,
            what="first harness exit",
        )
        path = self.work / "run/delegate.json"
        data = json.loads(path.read_text())
        self.assertEqual(data.pop("installation"), str(self.a))
        path.write_text(json.dumps(data))
        native = self.hooks()[-1]["payload"]["session_id"]
        transcript = Path(self.hooks()[-1]["payload"]["transcript_path"])
        history = transcript.read_text()
        shutil.rmtree(self.a)
        self.scenario.write_text(
            json.dumps({"tui_turns": [[mark("start", "recovered"), block(DONE)]]})
        )
        resumed = Record.parse(
            self.delegate(self.b, "resume", self.work, "--input", "continue").stdout
        )
        self.assertEqual(resumed.sid, record.sid)
        live_tmux.wait(lambda: self.finished(2), timeout=30, what="legacy recovery write-back")
        self.assertEqual(self.hooks()[-1]["payload"]["session_id"], native)
        self.assertEqual({row["exit"] for row in self.hooks()}, {0})
        self.assertTrue(transcript.read_text().startswith(history))
        self.assertEqual(self.saved.read_text(), "saved work")
        self.assertEqual((self.work / "out.md").read_text(), DONE["deliverable"])
        self.delegate(self.b, "close", self.work)
        live_tmux.wait(lambda: not list(self.snapshots.iterdir()), what="resumed copy cleanup")
