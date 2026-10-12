"""router.py as a subprocess on the Driver: run.py in its detached session on a private tmux server, fake_claude as
runtime claude, FakeLinear over HTTP. A claim runs to In Review; a crash or a Linear failure mid write-back resumes the
same session; a Linear outage stops a tick and --issue; a tick's usage gate; a long description reaches the run; a
SIGTERM'd run.py ends its run; the researcher's gate runs on a session's PYTHONPATH."""

import os
import shlex
import signal
import subprocess
import sys
import unittest

from agent_pm_orchestrator import config

from new.tests import live_tmux
from new.tests.case import ROOT
from new.tests.integration.delegate.cli_integration_test import mark, option
from new.tests.integration.orchestrator.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PROJECT,
    REPO,
    TIMEOUT,
    Flow,
    TmuxFlow,
)
from new.tests.integration.orchestrator.segments import FIRST_WRITE, agent_calls, lookup
from new.tests.unit.delegate.runner_test import block

ENGINEER = ACCOUNTS["engineer"]
ID, TITLE, BRANCH = "TASK-7", "ENG: Session registry", "TASK-7-session-registry"
PR = f"https://github.com/{REPO}/pull/1"
STARTED, READY = "Building the session registry.", "Added the session registry."
START = mark("start", STARTED)
DONE = block({"status": "done", "title": "Session registry", "summary": READY, "url": PR})
LEDGER = ["start", "comment", f"attach:{PR}", "move:in_review"]
ATTACHMENT = {"url": PR, "title": "Session registry"}
LONG = "\n".join(f"Requirement {n}: the registry keeps session {n} → its run." for n in range(400))
HARNESS_OPS = {
    "linear.Q_TEAM",
    "linear.Q_TASK_GROUP",
    "linear.Q_USER",
    "router.issues",
    "router.issues+relations",
    "router.Q_RECHECK",
    "linear.M_STATE",
    "issues.Q_ISSUE",
    "sessions.M_UPDATE",
    "linear.M_COMMENT_ID",
    "linear.Q_COMMENT",
}
ENGINEER_OPS = {
    "linear.M_COMMENT_ID",
    "writeback.Q_ATTACHED",
    "linear.M_SUBSCRIBE",
    "writeback.M_ATTACH",
    "linear.Q_ISSUE_STATE",
    "linear.M_STATE",
}


def gone(pid):
    res = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=TIMEOUT
    )
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


def usage(five_hour):
    return {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": five_hour}}}


class Router(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.fake.issue(
            TITLE,
            identifier=ID,
            assignee=self.ids[ENGINEER],
            project=PROJECT,
            description="Add a session registry.",
        )

    def claim(self, mode="start", *argv):
        res = self.router(*(argv or ("--issue", ID)))
        self.assertEqual(res.returncode, 0, res.stderr)
        last = self.runs()[-1]
        self.assertEqual(
            (last["kind"], last["issue"], last["role"], last["runtime"]),
            (mode, ID, "engineer", "claude"),
        )
        return last["sid"]

    def started(self, n=1):
        """The first n agent runs' fake_claude calls, once logged."""
        return live_tmux.wait(
            lambda: (calls := agent_calls(self.calls()))[n - 1 :] and calls,
            TIMEOUT,
            f"fake claude's start #{n}",
        )

    def input(self, sid):
        """run.jsonl's input record of sid's first run."""
        records = self.records(ID)
        at = next(i for i, r in enumerate(records) if r["kind"] == "session" and r["sid"] == sid)
        return next(r["text"] for r in records[at:] if r["kind"] == "input")

    def assert_built(self, sid):
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(
            self.history(ID),
            [("todo", "in_progress", HARNESS_EMAIL), ("in_progress", "in_review", ENGINEER)],
        )
        (author, session), *rest = self.comments(ID)
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertRegex(session, rf"^Run {sid} · done · .* · exit 0\n")
        self.assertEqual(
            rest, [(ENGINEER, f"Build started: {STARTED}"), (ENGINEER, f"Build ready: {READY}")]
        )
        self.assertEqual(self.attachments(ID), [ATTACHMENT])
        self.assertEqual(self.subscribers(ID), [HUMAN])
        self.assertEqual(self.ledger(ID), {sid: LEDGER})
        [running] = [v["b"] for v in self.sent("linear.M_COMMENT_ID", HARNESS_EMAIL)]
        self.assertTrue(running.startswith(f"Run {sid} · running · "), running)

    def test_claim_to_in_review(self):
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual(
            self.runs(),
            [{"kind": "start", "issue": ID, "sid": sid, "role": "engineer", "runtime": "claude"}],
        )
        lines = self.input(sid).splitlines()
        self.assertIn(f"Repo: {REPO}", lines)
        self.assertIn(f"Branch: {BRANCH}", lines)
        self.assertEqual(self.problems(), [FIRST_WRITE])
        self.assertEqual(
            {(r.op, r.account) for r in self.requests()},
            {(op, HARNESS_EMAIL) for op in HARNESS_OPS} | {(op, ENGINEER) for op in ENGINEER_OPS},
        )
        self.assertTrue(
            self.sent("sessions.M_UPDATE", HARNESS_EMAIL)[-1]["b"].startswith(
                f"Run {sid} · done · "
            )
        )

    def test_a_crash_mid_run_resumes_the_session(self):
        self.scene([START], hang=True)
        sid = self.claim()
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        driver = self.driver("engineer", ID)
        pane = self.server.tmux("list-panes", "-t", f"={driver}", "-F", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        os.kill(int(pane.stdout), signal.SIGKILL)
        try:
            os.kill(self.started()[0]["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        live_tmux.wait(lambda: not self.live(driver), TIMEOUT, f"{driver} to end")
        self.assertEqual(self.state(ID), "in_progress")
        [(author, session)] = [c for c in self.comments(ID) if c[1].startswith(f"Run {sid} · ")]
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertTrue(session.startswith(f"Run {sid} · running · "), session)
        self.assertEqual(self.ledger(ID), {sid: ["start"]})
        self.assertEqual([e for e in self.logged() if e["kind"] == "end"], [])

        self.scene([START, DONE])
        self.assertEqual(self.claim("resume"), sid)
        self.wait_end(sid, ID, ends=1)
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)]
        )
        first, second = self.started(2)
        self.assertEqual(option(first, "--session-id"), sid)
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (sid, None))
        self.assert_built(sid)
        started = [
            v
            for v in self.sent("linear.M_COMMENT_ID", ENGINEER)
            if v["b"].startswith("Build started")
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(self.problems(), [FIRST_WRITE, lookup(ENGINEER)])

    def test_a_linear_failure_mid_write_back_resumes_the_session(self):
        self.fake.fail("linear.M_STATE", "503", account=ENGINEER)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_progress")
        self.assertEqual(self.ledger(ID), {sid: LEDGER[:3]})
        self.assertEqual(self.comments(ID)[-1], (ENGINEER, f"Build ready: {READY}"))
        self.assertEqual(self.attachments(ID), [ATTACHMENT])
        [error] = [e for e in self.logged() if e["kind"] == "step-error"]
        self.assertEqual(
            (error["src"], error["issue"], error["sid"], error["step"]),
            ("writeback", ID, sid, "move:in_review"),
        )
        self.assertIn("503", error["error"])

        self.assertEqual(self.claim("resume"), sid)
        self.wait_end(sid, ID, ends=2)
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)]
        )
        self.assert_built(sid)
        ready = [
            v
            for v in self.sent("linear.M_COMMENT_ID", ENGINEER)
            if v["b"].startswith("Build ready")
        ]
        self.assertEqual(len(ready), 1)
        self.assertEqual(len(self.sent("writeback.M_ATTACH", ENGINEER)), 1)
        self.assertEqual(self.problems(), [FIRST_WRITE, ("linear.M_STATE", ENGINEER, "503")])

    def outage(self, *argv):
        self.fake.fail("linear.Q_TEAM", "503")
        res = self.router(*argv)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", ""))
        self.assertEqual(
            [(r.op, r.account, r.result) for r in self.requests()],
            [("linear.Q_TEAM", HARNESS_EMAIL, "503")],
        )
        self.assertEqual(
            [{k: v for k, v in e.items() if k != "ts"} for e in self.logged()],
            [{"src": "router", "kind": "linear-error", "op": "teams", "status": 503}],
        )
        self.assertEqual(self.state(ID), "todo")

    def test_a_linear_outage_stops_the_tick(self):
        self.outage("--now")

    def test_a_linear_outage_stops_issue_and_the_rerun_claims_it(self):
        self.outage("--issue", ID)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual([e["kind"] for e in self.logged()].count("linear-error"), 1)

    def test_a_tick_at_95_percent_usage_skips_the_claim(self):
        self.scene([START, DONE], rate_limit=usage(0.95))
        res = self.router("--now")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertEqual(
            self.said(),
            [f"router usage-skip {ID} runtime=claude reason=usage usage=five_hour=95% weekly=none"],
        )
        self.assertEqual((self.state(ID), self.runs()), ("todo", []))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(agent_calls(self.calls()), [])
        self.assertEqual(self.sent("linear.M_STATE"), [])

    def test_a_tick_at_50_percent_usage_claims_and_finishes(self):
        self.scene([START, DONE], rate_limit=usage(0.5))
        sid = self.claim("start", "--now")
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual(
            [line for line in self.said() if line.startswith("router ")],
            [
                "router plan mode=new queue=1",
                f"router pick {ID} queue=1",
                f"router claim {ID} role=engineer runtime=claude",
                f"router launch {ID} exit=0",
            ],
        )
        calls = self.calls()
        self.assertEqual([option(c, "--session-id") for c in agent_calls(calls)], [sid])
        self.assertEqual(len(calls), 2)  # and the usage probe

    def test_a_long_description_reaches_the_run_intact(self):
        with self.fake.lock:
            self.fake.find(ID)["description"] = LONG
        self.assertGreaterEqual(len(LONG.encode()), 20_000)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assertIn(f"\n\n{LONG}\n\n", self.input(sid))
        self.assert_built(sid)

    def test_a_sigtermed_run_ends_its_run_and_its_agent(self):
        self.scene([START], hang=True)
        sid = self.claim()
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        [agent] = self.started()
        driver = self.driver("engineer", ID)
        pane = self.server.tmux("list-panes", "-t", f"={driver}", "-F", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        os.kill(int(pane.stdout), signal.SIGTERM)
        self.wait_end(sid, ID)
        [end] = [e for e in self.logged() if (e["src"], e["kind"]) == ("run", "end")]
        self.assertEqual((end["issue"], end["exit"]), (ID, 128 + signal.SIGTERM))
        live_tmux.wait(lambda: gone(agent["pid"]), TIMEOUT, f"fake claude {agent['pid']} to end")
        [(author, session)] = [c for c in self.comments(ID) if c[1].startswith(f"Run {sid} · ")]
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertRegex(session, rf"^Run {sid} · interrupted · .* · exit {128 + signal.SIGTERM}\n")
        self.assertEqual((self.state(ID), self.ledger(ID)), ("in_progress", {sid: ["start"]}))


class Gate(Flow):
    SHIMS = ("claude", "python3")

    def test_the_researchers_gate_on_a_sessions_core_only_pythonpath(self):
        shim = os.path.join(self.bin, "python3")
        with open(shim, "w") as f:
            f.write(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(shim, 0o755)
        self.guard()
        self.scene([], rate_limit=usage(0.5))
        res = subprocess.run(
            ["sh", "-c", config.load().roles["researcher"].run.gate],
            cwd=self.root,
            env={**self.env(), "PYTHONPATH": ROOT},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertNotIn("ModuleNotFoundError", res.stderr)
        self.assertEqual(
            (res.returncode, res.stdout), (0, "five_hour=50% weekly=none\n"), res.stderr
        )


if __name__ == "__main__":
    unittest.main()
