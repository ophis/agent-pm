import copy
import os
import re
import time
from datetime import UTC, datetime, timedelta

from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.flows.expectations import BoardExpectations
from orchestrator_tests.integration.flows.fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PROJECT,
    REPO,
    RESEARCH_DIR,
    TIMEOUT,
    TmuxFlow,
)
from orchestrator_tests.integration.flows.types import (
    BUILD,
    DIRS,
    FIRST_WRITE,
    INSTRUCTIONS,
    REPORT,
    RESEARCH,
    RUN_KEYS,
    RUN_STEPS,
    RUNTIME,
    TEXT_KINDS,
    BoardChanges,
    Pending,
    Script,
    agent_calls,
    lookup,
)

from delegate.tests.replies import block, mark, option
from session.tests import live_tmux


class Segment(BoardExpectations):
    def agent_script(self, role, outcome, ident, url=None):
        """outcome: done, needs_input, failed, or none (no outcome block)."""
        self.n_scripts += 1
        n = self.n_scripts
        steps = [mark("start", f"Starting run #{n}.")]
        if outcome == "none":
            return Script(outcome, steps, None, {})
        report = {
            "status": outcome,
            "title": f"Session registry run {n}",
            "summary": f"Summary #{n}.",
        }
        if outcome == "done":
            report["url"] = url or (
                f"https://github.com/{REPO}/pull/{n}"
                if config.TASKS[role].kind == "build"
                else self.doc_url(f"{DIRS[role]}{datetime.now(UTC):%Y-%m-%d}-{ident}-run-{n}.md")
            )
        if outcome == "needs_input":
            report["questions"] = [f"Question #{n}?"]
        text = report["questions"][0] if outcome == "needs_input" else report["summary"]
        return Script(outcome, [*steps, block(report)], report, {text: TEXT_KINDS[outcome]})

    def launch(self, ident, role, script, *, resumed):
        """router.py --issue ident with script behind a new gate, until its fake_claude waits there
        and run.py's session comment says running; returns the runs.jsonl line, fake_claude's call
        and the events since.
        """
        if self.pending:
            self.fail(
                f"a claim or resume of {ident} while {self.pending.ident}"
                f"'s run is pending: agent_run() first"
            )
        self.n_gates += 1
        gate = os.path.join(self.root, f"gate-{self.n_gates}")
        self.scene(script.steps, gate=gate)
        calls, events, lines = len(agent_calls(self.calls())), len(self.logged()), len(self.runs())
        res = self.router("--issue", ident)
        if res.returncode != 0:
            self.fail(
                f"router.py exit {res.returncode}: {res.stderr.strip()!r}; events "
                f"{self.logged()[events:]}"
            )
        added = self.runs()[lines:]
        self.assertEqual(len(added), 1, f"runs.jsonl lines added: {added}")
        line = added[0]

        def started():
            try:
                return agent_calls(self.calls())[calls:]
            except ValueError:  # a line half appended
                return None

        call = live_tmux.wait(started, TIMEOUT, "the fake claude's start")[0]
        live_tmux.wait(
            lambda: self.session_status(ident, line["sid"]) == "running",
            TIMEOUT,
            f"{line['sid']}'s session comment to say running",
        )
        logged = self.logged()[events:]
        self.assertEqual(
            [
                (e["src"], e["kind"])
                for e in logged
                if e.get("issue") == ident and e["kind"].startswith("tui-")
            ],
            [],
        )
        self.texts.update(script.texts)
        self.pending = Pending(ident, role, line["sid"], script, gate, resumed)
        return line, call, logged

    def agent_run(self):
        p = self.pending
        if p is None:
            self.fail("agent_run() without a pending claim or resume")
        with self.step(RUN_STEPS[p.script.outcome]):
            with open(p.gate, "w"):
                pass
            ends = sum(line[0] != "limit" and line[2] == p.sid for line in self.want_runs)
            self.wait_end(p.sid, p.ident, ends, p.role)
            self.pending = None
            want, task, account, report = (
                self.want[p.ident],
                config.TASKS[p.role],
                ACCOUNTS[p.role],
                p.script.report,
            )
            self.expect_session(p.ident, p.sid, report["status"] if report else "done")
            ledgered = want.ledger.get(p.sid, [])
            first = "start" not in ledgered
            comments, steps, changes = [(account, "start")] if first else [], ["start"], {}
            if report:
                outcome, url = report["status"], report.get("url")
                state = "in_review" if outcome != "failed" or p.resumed else task.failed_new
                if outcome == "done" and task.retitle:
                    steps.append("retitle")
                    changes["title"] = f"{task.prefix}: {report['title']}"
                steps.append("comment")
                comments += [(account, k) for k in p.script.texts.values()]
                if outcome == "done" and url:
                    steps.append(f"attach:{url}")
                    if all(a["url"] != url for a in want.attachments):
                        changes["attachments"] = [{"url": url, "title": report["title"]}]
                steps.append(f"move:{state}")
                if state == "in_review" and HUMAN not in want.subscribers:
                    changes["subscribers"] = [HUMAN]
                changes.update(state=state, history=[("in_progress", state, account)])
            if p.resumed:
                posts = [s for s in ("start", "comment") if s in steps and s not in ledgered]
                self.expect_problem(*[lookup(account)] * len(posts))
            self.expect(p.ident, BoardChanges(comments=comments, ledger={p.sid: steps}, **changes))

    def backlog_issue(self, role, title, description, labels=()):
        ident = self.fake.issue(
            title,
            seed=fake_linear.IssueSeed(
                state="backlog",
                assignee=self.ids[ACCOUNTS[role]],
                project=PROJECT,
                description=description,
                labels=labels,
            ),
        )["identifier"]
        self.track(ident, state="backlog", title=title)
        return ident

    def seed_backlog(self, role, title, description, labels=()):
        with self.step("seed: backlog"):
            ident = self.backlog_issue(role, title, description, labels)
        return ident

    def refused(self, ident, note=""):
        """router.py --issue ident must exit 1; returns the events it logged."""
        events = len(self.logged())
        res = self.router("--issue", ident)
        added = self.logged()[events:]
        if res.returncode != 1:
            self.fail(
                "; ".join(
                    [
                        f"router.py exit {res.returncode}: {res.stderr.strip()!r}",
                        f"events {added}",
                        *([note] if note else []),
                    ]
                )
            )
        return added

    def backlog_not_claimed(self, ident):
        with self.step("router: backlog not claimed"):
            self.assertEqual(
                [(e["src"], e["kind"], e.get("issue")) for e in self.refused(ident)],
                [("router", "pick-none", ident)],
            )

    def human_move(self, ident, to, name):
        with self.step(name):
            self.fake.move(ident, to, actor=self.ids[HUMAN])
            self.expect(
                ident, BoardChanges(state=to, history=[(self.want[ident].state, to, HUMAN)])
            )

    def human_say(self, ident, text, to, name):
        with self.step(name):
            self.fake.comment(ident, self.ids[HUMAN], text)
            self.fake.move(ident, to, actor=self.ids[HUMAN])
            self.expect(
                ident,
                BoardChanges(
                    state=to,
                    history=[(self.want[ident].state, to, HUMAN)],
                    comments=[(HUMAN, f"human: {text}")],
                ),
            )

    def handoff(self, ident, text):
        self.human_say(ident, text, "handoff", "human: handoff")

    def claim(self, ident, role, script, task=None):
        with self.step("router: claim"):
            line, call, events = self.launch(ident, role, script, resumed=False)
            sid = line["sid"]
            self.assertEqual(
                (line["kind"], line["issue"], line["role"], line["runtime"]),
                ("start", ident, role, RUNTIME),
            )
            self.assertNotIn(sid, self.sids())
            self.assertEqual(option(call, "--session-id"), sid)
            mine = [e for e in events if e.get("issue") == ident]
            self.assertEqual([e for e in mine if e["kind"] == "transient"], [])
            self.assertEqual(
                [(e["role"], e.get("task"), e["runtime"]) for e in mine if e["kind"] == "claim"],
                [(role, task, RUNTIME)],
            )
            self.expect_runs(("start", ident, sid, role, RUNTIME))
            self.expect(
                ident,
                BoardChanges(state="in_progress", history=[("todo", "in_progress", HARNESS_EMAIL)]),
            )
            self.expect_session(ident, sid, "running")
            self.expect_problem(FIRST_WRITE)
        return sid

    def resume(self, ident, role, sid, script):
        with self.step("router: resume"):
            line, call, _ = self.launch(ident, role, script, resumed=True)
            self.assertEqual(
                tuple(line[k] for k in RUN_KEYS), ("resume", ident, sid, role, RUNTIME)
            )
            self.assertEqual((option(call, "--resume"), option(call, "--session-id")), (sid, None))
            self.expect_runs(("resume", ident, sid, role, RUNTIME))
            self.expect_session(ident, sid, "running")
        return sid

    def promote_now(self):
        return TmuxFlow.promote(self, "--now")

    def promote(self, ident, role, nxt):
        with self.step("promote"):
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))
            child, title = self.assert_child(ident, role, nxt)
            self.expect(
                ident,
                BoardChanges(
                    state="done",
                    history=[("handoff", "done", HARNESS_EMAIL)],
                    comments=[(HARNESS_EMAIL, f"promoted {child}")],
                ),
            )
            self.track(child, state="todo", title=title)
        return child

    def promote_nothing(self, ident):
        with self.step("promote: nothing"):
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))
            self.assertEqual(self.sent("promote.M_CREATE"), [])
            self.assertNotIn("handoff", [to for _, to, _ in self.history(ident)])

    def assert_child(self, ident, role, nxt):
        """ident's one related child: nxt's Todo issue in its project, titled and described as
        promote makes it; returns its identifier and title.
        """
        with self.fake.lock:
            src = copy.deepcopy(self.fake.find(ident))
            related = [b for t, a, b in self.fake.relations if t == "related" and a == src["id"]]
            self.assertEqual(len(related), 1, f"issues related from {ident}: {related}")
            child = copy.deepcopy(self.fake.find(related[0]))
            name = self.fake.users[self.ids[HUMAN]]["name"]
        prefix, want = config.TASKS[role].prefix, self.want[ident]
        title = f"{config.TASKS[nxt].prefix}: " + (
            want.title.removeprefix(f"{prefix}: ") if prefix else want.title
        )
        self.assertEqual(
            (
                child["assignee"],
                fake_linear.NAMES[child["state"]],
                child["project"],
                child["title"],
            ),
            (self.ids[ACCOUNTS[nxt]], "todo", src["project"], title),
        )
        cutoff = max(
            h["createdAt"]
            for h in src["history"]
            if fake_linear.NAMES[h["toStateId"]] == "in_review"
            and fake_linear.NAMES[h["fromStateId"]] != "handoff"
        )
        said = [
            f"{name}, {c['createdAt']}:\n{c['body']}"
            for c in src["comments"]
            if c["user"] == self.ids[HUMAN] and c["createdAt"] > cutoff
        ]
        head = [
            f"Handoff from {ident}: {src['url']}",
            *(
                ["## Source\n" + "\n".join(f"- {a['title']}: {a['url']}" for a in want.attachments)]
                if want.attachments
                else []
            ),
            *(["## Instructions\n" + "\n\n".join(said)] if said else []),
        ]
        self.assertEqual(child["description"].partition("\n\n## Comments\n")[0], "\n\n".join(head))
        made = [
            (r.account, r.variables["in"]["id"])
            for r in self.requests()
            if r.op == "promote.M_CREATE"
        ]
        self.assertEqual({a for a, _ in made}, {HARNESS_EMAIL})
        ids = [i for _, i in made]
        self.assertIn(child["id"], ids)
        self.assertEqual(len(ids), len(set(ids)), f"promote.M_CREATE made an issue twice: {ids}")
        return child["identifier"], title

    def assert_pm_start(self, ident):
        self.assertIn(ident, self.want)
        rec = self.issue(ident)
        self.assertEqual(
            (rec["assignee"], fake_linear.NAMES[rec["state"]], rec["project"]),
            (self.ids[ACCOUNTS["pm"]], "todo", PROJECT),
        )

    def moved_to_todo(self, ident):
        return linear.last_move(
            self.issue(ident)["history"], {fake_linear.IDS["todo"]}, {self.ids[HUMAN]}
        )

    def wait_next_second(self, ident):
        # router.attempt_count counts the runs.jsonl lines strictly after moved_to_todo, and their
        # ts is to the second (linear.stamp), so a claim within its second is no attempt
        time.sleep(
            max(
                0,
                (
                    self.moved_to_todo(ident).replace(microsecond=0)
                    + timedelta(seconds=1)
                    - datetime.now(UTC)
                ).total_seconds(),
            )
        )

    def attempts(self, ident):
        moved, entries = (
            self.moved_to_todo(ident),
            runs.parse_log(os.path.join(self.logs, "runs.jsonl")),
        )
        return (
            f"{routing.attempt_count(entries, ident, moved)}"
            f" attempts since the human's move to Todo at {moved.isoformat()}"
            f"; runs.jsonl at {[e.ts.isoformat() for e in entries]}"
        )

    def prompt(self, ident, sid):
        """The first input record after sid's first session record."""
        records = self.records(ident)
        at = next(
            (i for i, r in enumerate(records) if r["kind"] == "session" and r["sid"] == sid), None
        )
        text = (
            None
            if at is None
            else next((r["text"] for r in records[at:] if r["kind"] == "input"), None)
        )
        if text is None:
            self.fail(f"no input record of {sid} in {ident}'s run.jsonl")
        return text

    def section(self, prompt, heading):
        # up to the next `## ` line, a fenced one too: no text a test publishes or reports may hold
        # one
        if m := re.search(rf"^## {re.escape(heading)}\n\n(.*?)(?=^## |\Z)", prompt, re.M | re.S):
            return m[1]
        self.fail(f"no `## {heading}` section in the prompt:\n{prompt}")

    def researcher_ready(self, labels=()):
        ident = self.seed_backlog("researcher", *RESEARCH, labels=labels)
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        return ident

    def researcher_done(self, ident, task=None):
        self.claim(ident, "researcher", self.agent_script("researcher", "done", ident), task=task)
        self.agent_run()
        self.handoff(ident, INSTRUCTIONS)
        return self.promote(ident, "researcher", "pm")

    def researcher_to_pm(self, labels=(), task=None):
        return self.researcher_done(self.researcher_ready(labels), task)

    def pm_ready(self):
        with self.step("seed: backlog"):
            src = self.fake.issue(
                RESEARCH[0],
                seed=fake_linear.IssueSeed(
                    state="done",
                    assignee=self.ids[ACCOUNTS["researcher"]],
                    project=PROJECT,
                    description=RESEARCH[1],
                ),
            )
            path = (
                f"{RESEARCH_DIR}{datetime.now(UTC):%Y-%m-%d}-{src['identifier']}"
                f"-session-registries.md"
            )
            report = {"url": self.doc_url(path), "title": RESEARCH[0]}
            self.publish(path, REPORT)
            with self.fake.lock:
                src["attachments"].append(dict(report))
                name = self.fake.users[self.ids[HUMAN]]["name"]
            self.track(src["identifier"], attachments=[report])
            ident = self.backlog_issue(
                "pm",
                f"{config.TASKS['pm'].prefix}: {RESEARCH[0]}",
                "\n\n".join(
                    [
                        f"Handoff from {src['identifier']}: {src['url']}",
                        f"## Source\n- {RESEARCH[0]}: {report['url']}",
                        f"## Instructions\n{name}, {fake_linear.stamp()}:\n{INSTRUCTIONS}",
                    ]
                ),
            )
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        return ident

    def pm_done(self, ident, url=None):
        self.claim(ident, "pm", self.agent_script("pm", "done", ident, url))
        self.agent_run()
        self.handoff(ident, BUILD)
        return self.promote(ident, "pm", "engineer")

    def pm_to_engineer(self, start=None):
        if start is None:
            return self.pm_done(self.pm_ready())
        self.assert_pm_start(start)
        return self.pm_done(start)
