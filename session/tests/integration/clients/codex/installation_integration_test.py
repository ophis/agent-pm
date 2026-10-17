"""Fixture cache removal preserves all interactive hooks and explicit worker recovery."""

import json
import os
import shutil
from pathlib import Path
from unittest import mock

from agent_pm.session import RunLog
from agent_pm.session.engine.config import read_config
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.session.engine.worker import Worker, WorkerState
from agent_pm.session.runtimes.tui import TuiRuntime
from agent_pm.utils import Record

from session.tests import live_tmux
from session.tests.installation import UpgradeCase


class Upgrade(UpgradeCase):
    def test_existing_live_stale_command_recovers_by_idle_worker_restart(self):
        class Legacy(HostedSession):
            pass

        self.scenario.write_text(json.dumps({"tui_turns": [["saved answer"], ["stale answer"]]}))
        environment = {**self.env, "PATH": str(self.a / "bin") + os.pathsep + self.env["PATH"]}
        with mock.patch.dict(os.environ, environment, clear=True):
            Legacy.configure_from(read_config())
            host = Legacy.host()
            worker = Worker(
                WorkerState(
                    "worker",
                    "11111111-2222-3333-4444-555555555555",
                    "codex",
                    str(self.work),
                    host.name,
                    flags=("-s", "danger-full-access"),
                    display="none",
                ),
                session_cls=Legacy,
            )
            spec = worker.state.spec(prompt="saved request", resume=False)
            setup = Legacy._config()
            pending = TuiRuntime._prepare(
                spec,
                setup.registry.client("codex"),
                registry=setup.registry,
                host=host,
                display="none",
            )

            def commit():
                pending.run.commit()
                RunLog(spec.workdir).note("session", **pending.prepared.info.persisted())

            # Construct a legacy launch record through the unchanged lower-level launch API.
            with worker.locked():
                worker._claim()
                worker._save()
                pending.store.start(
                    pending.state,
                    commit,
                    lambda: host.terminal(
                        "worker", pending.launch, display="none", metadata=pending.metadata
                    ),
                )
                host.attach("worker").publish(worker.state.record())
        live_tmux.wait(
            lambda: any(row["hook"] == "Stop" for row in self.hooks()), what="legacy worker idle"
        )
        native = self.hooks()[-1]["payload"]["session_id"]
        transcript = Path(self.hooks()[-1]["payload"]["transcript_path"])
        saved = self.work / "saved.txt"
        saved.write_text("saved work")
        shutil.rmtree(self.a)
        self.cli(self.b, "send", "worker", "stale request")
        live_tmux.wait(
            lambda: any(row["hook"] == "Stop" and row["exit"] == 127 for row in self.hooks()),
            what="stale live hook failure",
        )
        history = transcript.read_text()
        self.scenario.write_text(json.dumps({"tui_turns": [["recovered"]]}))
        resumed = Record.parse(self.cli(self.b, "restart", "worker").stdout)
        self.assertEqual(resumed.sid, worker.state.sid)
        before = len(self.hooks())
        self.cli(self.b, "send", "worker", "recovered request")
        live_tmux.wait(
            lambda: any(row["hook"] == "Stop" for row in self.hooks()[before:]),
            what="recovered hook",
        )
        self.assertEqual({row["exit"] for row in self.hooks()[before:]}, {0})
        self.assertEqual(self.hooks()[-1]["payload"]["session_id"], native)
        self.assertTrue(transcript.read_text().startswith(history))
        self.assertEqual(saved.read_text(), "saved work")
        self.cli(self.b, "close", "worker")
        live_tmux.wait(
            lambda: not list(self.snapshots.iterdir()), what="recovered snapshot cleanup"
        )

    def test_all_hooks_after_fixture_removal_and_same_conversation_after_restart(self):
        labels = (
            "SessionStart",
            "UserPromptSubmit",
            "PostToolUse",
            "PermissionRequest",
            "Interrupt",
        )
        steps = [
            {"kind": "pause", "file": str(self.gate)},
            *({"kind": "hook", "label": label} for label in labels),
            "saved answer",
        ]
        self.scenario.write_text(json.dumps({"tui_turns": [steps]}))
        started = self.cli(
            self.a,
            "start",
            "worker",
            "--client",
            "codex",
            "--cwd",
            self.work,
            "--prompt",
            "saved request",
            "--display",
            "none",
            "--",
            "-s",
            "danger-full-access",
        )
        record = Record.parse(started.stdout)
        live_tmux.wait(
            lambda: any(row.get("hook") == "UserPromptSubmit" for row in self.hooks()),
            what="initial worker hook",
        )
        before = len(self.hooks())
        journal = self.home / ".agent-pm/tui/worker/hooks.jsonl"
        journal_before = len(journal.read_text().splitlines())
        state = json.loads((self.home / ".agent-pm/tui/worker/session.json").read_text())
        self.assertNotIn(str(self.a), json.dumps(state["client_hook"]))
        self.assertEqual(state["client_hook"]["module"], "agent_pm.session.clients.codex.tui")
        saved = self.work / "saved.txt"
        saved.write_text("saved work")
        shutil.rmtree(self.a)
        self.gate.touch()
        live_tmux.wait(
            lambda: any(row.get("hook") == "Stop" for row in self.hooks()[before:]),
            what="all post-upgrade hooks",
        )
        delivered = self.hooks()[before:]
        self.assertEqual([row["hook"] for row in delivered], [*labels, "Stop"])
        self.assertEqual({row["exit"] for row in delivered}, {0}, delivered)
        received = [json.loads(line) for line in journal.read_text().splitlines()][journal_before:]
        self.assertEqual([row["label"] for row in received], [*labels, "Stop"])
        self.assertEqual(
            [row["payload"] for row in received], [row["payload"] for row in delivered]
        )
        native = delivered[-1]["payload"]["session_id"]
        transcript = Path(delivered[-1]["payload"]["transcript_path"])
        history = transcript.read_text()
        self.assertEqual(len(list(self.snapshots.iterdir())), 1)
        self.scenario.write_text(json.dumps({"tui_turns": [["recovered"]]}))
        restarted = Record.parse(self.cli(self.b, "restart", "worker").stdout)
        self.assertEqual(restarted.sid, record.sid)
        self.cli(self.b, "send", "worker", "next request")
        live_tmux.wait(
            lambda: len([row for row in self.hooks() if row["hook"] == "Stop"]) >= 2,
            what="restarted worker hook",
        )
        self.assertEqual(self.hooks()[-1]["payload"]["session_id"], native)
        self.assertTrue(transcript.read_text().startswith(history))
        self.assertEqual(saved.read_text(), "saved work")
        self.assertEqual({row["exit"] for row in self.hooks()}, {0})
        self.cli(self.b, "close", "worker")
        live_tmux.wait(lambda: not list(self.snapshots.iterdir()), what="worker snapshot cleanup")
