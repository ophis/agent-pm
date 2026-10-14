"""v1 router behaviors, each against its v1 source (`v1-runtime`'s CLAUDE.md › Architecture or a v1
test, named in the test's docstring), end to end: bin/agent-pm-linear router as a real process
(BinFlow), fake_claude as client claude, FakeLinear over HTTP, the runs on a private tmux server.
"""

import os
import unittest

from agent_pm.linear_orchestrator.engine.router import BUSY
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    PROJECT,
    REPO,
    TIMEOUT,
    BinFlow,
    usage,
)
from orchestrator_tests.integration.segments import agent_calls

from delegate.tests.replies import block, mark
from session.tests import live_tmux

ENGINEER = ACCOUNTS["engineer"]
ID, OTHER = "TASK-7", "TASK-8"
STARTED, READY = "Building the session registry.", "Added the session registry."
PR = f"https://github.com/{REPO}/pull/1"
START = mark("start", STARTED)
DONE = block({"status": "done", "title": "Session registry", "summary": READY, "url": PR})
# a hang answer's delay: the first router holds the lock that long
HOLD = 10


class Parity(BinFlow):
    def seed(self, ident, title, assignee=ENGINEER, **fields):
        self.fake.issue(
            title,
            identifier=ident,
            assignee=self.ids[assignee],
            project=PROJECT,
            description=f"{title}.",
            **fields,
        )

    def tick(self):
        res = self.router("--now")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())

    def routed(self):
        """The router's orchestrator.jsonl lines: a run's write-back may log in between."""
        return [line for line in self.said() if line.startswith("router ")]

    def started(self, n=1):
        """The first n agent runs' fake_claude calls, once logged."""
        return live_tmux.wait(
            lambda: (calls := agent_calls(self.calls()))[n - 1 :] and calls,
            TIMEOUT,
            f"fake claude's start #{n}",
        )


class Lock(Parity):
    def setUp(self):
        super().setUp()
        self.seed(ID, "ENG: Session registry")

    def hold(self):
        """A tick in the router lock, its first Linear call (the team) answered HOLD s late."""
        self.fake.fail("linear.Q_TEAM", "hang", seconds=HOLD)
        self.scene([START, DONE], rate_limit=usage(0.5))
        first = self.start("router", "--now")
        live_tmux.wait(self.requests, TIMEOUT, "the first router's team query")
        return first

    def assert_skipped(self, first, said):
        self.assertIsNone(first.poll(), "the first router ended before the second ran")
        self.assertEqual(self.said(), [said])
        self.assertEqual([r.op for r in self.requests()], ["linear.Q_TEAM"])
        self.assertEqual(agent_calls(self.calls()), [])
        out, err = first.communicate(timeout=TIMEOUT)
        self.assertEqual((first.returncode, out, err), (0, "", ""))
        [row] = self.runs()
        self.assertEqual((row["kind"], row["issue"]), ("start", ID))
        self.wait_end(row["sid"], ID)
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.said().count(said), 1)

    def test_a_tick_while_another_router_holds_the_lock_logs_one_skip_and_does_nothing(self):
        """v1 CLAUDE.md › Architecture: the router lock keeps one router at a time; held by
        another router → one `skip` event, nothing else.
        """
        first = self.hold()
        res = self.router("--now")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assert_skipped(first, f"router skip reason={BUSY}")

    def test_an_issue_run_while_another_router_holds_the_lock_logs_one_skip(self):
        """v1 CLAUDE.md › Architecture: `--issue` takes the router lock too; held → one `skip`."""
        first = self.hold()
        res = self.router("--issue", ID)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (1, "", f"router.py: {BUSY}; try again\n"),
        )
        self.assert_skipped(first, f"router skip {ID} reason={BUSY}")


class MaxRuns(Parity):
    def setUp(self):
        super().setUp()
        local = os.path.join(self.agent_pm, "config.local.toml")
        with open(local) as f:
            text = f.read()
        key = 'key = "linear-api-key-engineer"\n'
        self.assertEqual(text.count(key), 1)
        with open(local, "w") as f:
            f.write(text.replace(key, key + "max_runs = 1\n"))
        self.seed(ID, "ENG: Session registry", priority=2)
        self.seed(OTHER, "ENG: Session index", priority=3)

    def test_a_live_agent_run_on_tmux_holds_the_roles_only_slot(self):
        """v1 CLAUDE.md › Architecture: a tick compares live tmux sessions with `max_runs`; the
        issue's driver session holds a slot while its driver runs, so a full role claims nothing.
        """
        gate = os.path.join(self.root, "gate")
        self.scene([START, DONE], gate=gate, rate_limit=usage(0.5))
        self.tick()
        [first] = self.runs()
        self.assertEqual((first["kind"], first["issue"]), ("start", ID))
        self.started()
        self.assertTrue(self.live(self.harness("engineer", ID, first["sid"])))

        said = self.routed()
        self.tick()
        self.assertEqual((self.routed(), self.runs(), self.state(OTHER)), (said, [first], "todo"))
        self.assertEqual(len(agent_calls(self.calls())), 1)

        with open(gate, "w"):
            pass
        self.wait_end(first["sid"], ID)
        self.tick()
        _, second = self.runs()
        self.assertEqual((second["kind"], second["issue"]), ("start", OTHER))
        self.wait_end(second["sid"], OTHER)
        self.assertEqual((self.state(ID), self.state(OTHER)), ("in_review", "in_review"))


if __name__ == "__main__":
    unittest.main()
