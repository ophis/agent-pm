"""attach adopts over the real agent-pm-session ls --here --records (FR-20, FR-21;
manager/tests/live_crew.py): orphans started from the manager by the layer commands themselves,
and a crew run killed after its starter printed the Record but before the commit.
"""

import json
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta

from agent_pm.manager import State
from agent_pm.manager.engine.crew import RECOVERY_AGE
from agent_pm.utils import Record

from delegate.tests.replies import mark
from manager.tests.live_crew import CREW, MANAGER, OTHER, LiveCrew
from session.tests import live_tmux

START = mark("start", "echoing")
# Runs the starter argv, then marks $1 and waits for $2: its Record printed, its crew run waiting.
WRAP = ("sh", "-c", 'e=$1 g=$2; shift 2; "$@"; : > "$e"; until [ -e "$g" ]; do sleep 0.05; done')


class OrphanTest(LiveCrew):
    def test_attach_adopts_the_matching_orphans_once_each_by_their_record_names(self):
        worker = self.orphan("o")
        work = self.workdir("r")
        argv = self.role_argv("r")[1:]
        events = os.path.join(self.root, "orphan-events")
        role = self.record(
            self.call("agent-pm-delegate", *argv, "--events", events, **self.scene("r", [START]))
        )
        self.ok(self.server.tmux("new-session", "-d", "-s", OTHER, *live_tmux.IDLE))
        self.server.attach(OTHER)
        self.orphan("x", OTHER)
        early = self.session(*self.worker_argv("dead")[1:], "--", "--bogus", **self.scene("dead"))
        self.assertEqual(early.returncode, 1, early.stderr)
        self.assertTrue(self.live("dead"))
        for _ in range(2):
            self.ok(self.crew("attach"))
            entries = self.roster()
            self.assertEqual(sorted(entries), sorted(("o", role.name)))
            self.assertEqual(
                {name: (e.state, e.record) for name, e in entries.items()},
                {"o": (State.LIVE, worker), role.name: (State.LIVE, role)},
            )
        self.assertEqual(role.workdir, work)


class CrashTest(LiveCrew):
    def crashed(self, name: str, argv, **scene: str) -> Record:
        """crew run `name` --note keep -- argv, killed once argv printed its Record; then attach,
        RECOVERY_AGE later. The starter's Record.
        """
        emitted, gate = (os.path.join(self.root, f"{name}.{n}") for n in ("emitted", "gate"))
        crew = subprocess.Popen(
            (*CREW, "run", name, "--note", "keep", "--", *WRAP, "wrap", emitted, gate, *argv),
            env=self.env(**self.crew_env(), **scene),
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(crew.wait)
        self.addCleanup(lambda: open(gate, "a").close())
        with self.poking():
            live_tmux.wait(lambda: os.path.exists(emitted), 60, what=f"{name}'s Record printed")
        crew.send_signal(signal.SIGKILL)
        crew.wait()
        intent = self.roster()[name]
        self.assertEqual((intent.state, intent.record), (State.STARTING, None))
        path = os.path.join(self.agent_pm, "managers", MANAGER, "roster.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        old = datetime.now(UTC) - timedelta(seconds=RECOVERY_AGE + 1)
        data["entries"][name]["started"] = old.isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.ok(self.crew("attach"))
        listed = self.ok(self.session("ls", "--here", "--records")).stdout.splitlines()
        (record,) = {r for r in map(Record.parse, listed) if r.sid == intent.sid}
        entries = self.roster()
        self.assertEqual(list(entries), [name])
        self.assertEqual(
            (entries[name].state, entries[name].record, entries[name].note),
            (State.LIVE, record, "keep"),
        )
        (status,) = (
            json.loads(line) for line in self.ok(self.crew("status", name)).stdout.splitlines()
        )
        self.assertEqual((status["name"], status["alive"]), (name, True))
        return record

    def test_a_worker_whose_crew_died_before_the_commit_is_adopted_under_its_alias(self):
        record = self.crashed("w", self.worker_argv("w1"), **self.scene("w"))
        self.assertEqual((record.kind, record.name), ("worker", "w1"))

    def test_a_role_run_whose_crew_died_before_the_commit_is_adopted_under_its_alias(self):
        record = self.crashed("r", self.role_argv("r"), **self.scene("r", [START]))
        self.assertEqual((record.kind, len(record.sessions)), ("role", 2))
