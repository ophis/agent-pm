"""The commands /agent-pm:tmux and /agent-pm:manage write, filled in as a commander would, run on
the real commands (manager/tests/live_crew.py): a worker's launch, reply, note, status,
gone-then-resume and stop; a role run's dry-run check; a role run that ends waiting, answered in
its own session; manage's Codex issue worker launch.
"""

import json
import os

from agent_pm.manager import State

from delegate.tests.integration.cli import ASKING, DONE
from delegate.tests.replies import block
from manager.tests.live_crew import ROLE, LiveCrew, has_pair, logged
from manager.tests.skills.tmux import example, fill
from session.tests.integration.adapters.codex import fake_tui


class SkillFlows(LiveCrew):
    def written(self, skill, prefix, *having, keep=(), env=None, **values):
        """skill's one command starting with prefix (and holding having), filled with values, run
        as the commander runs it.
        """
        argv = fill(example(skill, prefix, *having), *keep, **values)
        crew = self.crew_env() if argv[0] == "agent-pm-crew" else {}
        return self.call(*argv, **crew, **(env or {}))

    def test_a_worker_launched_followed_up_resumed_and_stopped_as_written(self):
        record = self.record(
            self.written(
                "tmux",
                "agent-pm-crew run <name> -- agent-pm-session start",
                keep=("--prompt",),
                env=self.scene("w1"),
                name="w1",
                client="claude",
                text="hi",
            )
        )
        self.assertEqual(self.roster()["w1"].state, State.LIVE)
        self.wait_event("w1", "done")
        reply = self.ok(self.written("tmux", "agent-pm-session reply", name="w1"))
        self.assertIn("Hi from w1.", reply.stdout)
        self.ok(self.written("tmux", "agent-pm-crew note", name="w1", text="TASK-1 it's"))
        self.assertEqual(self.roster()["w1"].note, "TASK-1 it's")
        status = self.ok(
            self.written("tmux", "agent-pm-crew status [", keep=("<name>",), name="w1")
        )
        self.assertTrue(json.loads(status.stdout)["alive"])

        self.kill("w1")
        attach = self.ok(self.written("tmux", "agent-pm-crew attach"))
        self.assertRegex(attach.stdout, r"(?m)^resume: agent-pm-crew resume .*\bw1$")
        resumed = self.record(
            self.written("tmux", "agent-pm-crew resume <name>", env=self.scene("w1"), name="w1")
        )
        self.assertEqual(resumed.sid, record.sid)
        self.assertTrue(self.live("w1"))

        self.ok(self.written("tmux", "agent-pm-crew stop <name>", name="w1"))
        self.assertNotIn("w1", self.roster())
        self.assertTrue(self.gone("w1"))

    def test_a_role_runs_dry_run_check_prints_its_plan_and_starts_nothing(self):
        work = self.workdir("r")
        os.makedirs(work)
        out = self.ok(
            self.written(
                "tmux",
                "agent-pm-delegate run --role",
                "--dry-run",
                role=ROLE,
                workdir=work,
                input="Hello, it's me.",
            )
        )
        self.assertEqual(json.loads(out.stdout)["mode"], "tui")
        self.assertEqual(logged(self.log("r")), [])

    def test_a_waiting_role_run_is_answered_in_its_session_as_written(self):
        work = self.workdir("r")
        launch = fill(
            example("tmux", "agent-pm-crew run <name> -- agent-pm-delegate run"),
            name="r",
            role=ROLE,
            workdir=work,
            input="Hello.",
        )
        argv = ["headless" if word == "tui" else word for word in launch]
        record = self.record(
            self.call(*argv, **self.crew_env(), **self.scene("r", [block(ASKING)]))
        )
        (harness,) = record.sessions
        self.wait_event(harness, "exited")
        self.ok(self.crew("attach"))
        self.assertEqual(self.roster()["r"].state, State.WAITING)

        answered = self.record(
            self.written(
                "tmux",
                "agent-pm-delegate run --role",
                "--sid",
                env=self.scene("r", [block(DONE)]),
                role=ROLE,
                workdir=work,
                mode="headless",
                events=self.events,
                sid=record.sid,
                answers="Say hello.",
            )
        )
        self.assertEqual(answered.sid, record.sid)
        self.wait_event(harness, "exited", count=2)
        self.assertTrue(has_pair(logged(self.log("r"))[-1]["argv"], "--resume", record.sid))
        status = json.loads(self.ok(self.crew("status", "r")).stdout)
        self.assertEqual(status["outcome"], "done")

    def test_manages_codex_issue_worker_launches_by_name_with_its_prompt(self):
        fake_tui.install(self.server.bin)
        never, scenario = (os.path.join(self.root, n) for n in ("never", "c1.codex.json"))
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump({"tui_turns": [[{"kind": "pause", "file": never}]]}, f)
        log = os.path.join(self.root, "c1.codex.jsonl")
        record = self.record(
            self.written(
                "manage",
                "agent-pm-crew run <name> -- agent-pm-session start",
                env={"FAKE_SCENARIO": scenario, "FAKE_LOG": log},
                name="c1",
                work_dir=self.cwd,
            )
        )
        self.assertEqual((record.kind, record.name, record.detail), ("worker", "c1", "codex"))
        [begun] = [start for start in logged(log) if start.get("mode") == "tui"]
        self.assertEqual(os.path.realpath(begun["cwd"]), os.path.realpath(self.cwd))
