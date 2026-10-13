"""The flows' Segment layer over TmuxFlow: each step changes the board (a seed, a human's move or
comment, a router.py --issue claim or resume, an agent run behind its fake_claude gate, a promote
tick), says what it expects of the tracked issues, runs.jsonl and Linear's failed requests, and
verify() compares them when the step ends.
"""

import contextlib
import copy
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from agent_pm.linear_orchestrator.engine import config, router, runs
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.integration import fake_linear
from orchestrator_test_helpers.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PRD_DIR,
    PROJECT,
    REPO,
    RESEARCH_DIR,
    TIMEOUT,
    TmuxFlow,
)

from delegate.tests.replies import block, mark, option
from session.tests import live_tmux

ROLE_OF = {a: r for r, a in ACCOUNTS.items()}
DIRS = {"researcher": RESEARCH_DIR, "pm": PRD_DIR}
TEXT_KINDS = {"done": "done", "needs_input": "question", "failed": "failed"}
RUN_STEPS = {
    "done": "run: in review",
    "needs_input": "run: needs input",
    "failed": "run: failed",
    "none": "run: no outcome",
}
RUNTIME = "claude"
RUN_KEYS = ("kind", "issue", "sid", "role", "runtime")
SESSION = re.compile(r"(running|done|failed|needs_input|interrupted|limit) · ")
# a session comment's first write: its update misses, then it is created by its id
FIRST_WRITE = ("sessions.M_UPDATE", HARNESS_EMAIL, "error")
HARNESS_TEXTS = {router.CAP_COMMENT: "cap", router.INTERRUPTED: "interrupted"}
RESEARCH = (
    "Session registries in agent harnesses",
    "How do agent harnesses keep track of their sessions?",
)
INSTRUCTIONS = "Write the PRD for a session registry."
REPORT = (
    "# Session registries in agent harnesses\n\nEach harness names a tmux session after its run."
)
BUILD = "Build the session registry."


def lookup(account):
    """A resumed run's look for an unledgered comment step's comment (FR-54): not found, then
    posted.
    """
    return ("linear.Q_COMMENT", account, "error")


def agent_calls(calls):
    """fake_claude's calls of agent runs: the usage probe's have neither --session-id nor --resume."""
    return [c for c in calls if {"--session-id", "--resume"} & set(c["argv"])]


@dataclass
class Want:
    state: str
    title: str
    history: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)


class Script(NamedTuple):
    outcome: str
    steps: list
    report: dict | None
    texts: dict


class Pending(NamedTuple):
    ident: str
    role: str
    sid: str
    script: Script
    gate: str
    resumed: bool


class Segment(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.want, self.want_runs, self.want_problems, self.texts = {}, [], [], {}
        self.n_steps = self.n_scripts = self.n_gates = 0
        self.pending = None

    @contextlib.contextmanager
    def step(self, name):
        self.n_steps += 1
        n = self.n_steps
        try:
            yield
            self.verify()
        except AssertionError as e:
            raise self.failureException(f"failed at step {n} ({name}): {e}") from e
        except Exception as e:
            e.add_note(f"failed at step {n} ({name})")
            raise

    def track(self, ident, **fields):
        rec = self.issue(ident)
        self.want[ident] = Want(
            **{"state": fake_linear.NAMES[rec["state"]], "title": rec["title"], **fields}
        )

    def expect(
        self,
        ident,
        *,
        state=None,
        title=None,
        history=(),
        comments=(),
        attachments=(),
        subscribers=(),
        ledger=None,
    ):
        want = self.want[ident]
        want.state = want.state if state is None else state
        want.title = want.title if title is None else title
        want.history += history
        want.comments += comments
        want.attachments += attachments
        want.subscribers += subscribers
        want.ledger.update(ledger or {})

    def expect_session(self, ident, sid, status):
        comments, entry = self.want[ident].comments, (HARNESS_EMAIL, f"session {sid} {status}")
        at = next(
            (
                i
                for i, (a, k) in enumerate(comments)
                if a == HARNESS_EMAIL and k.startswith(f"session {sid} ")
            ),
            None,
        )
        if at is None:
            comments.append(entry)
        else:
            comments[at] = entry

    def expect_runs(self, *lines):
        self.want_runs += lines

    def expect_problem(self, *problems):
        self.want_problems += problems

    def sids(self):
        return {line[2] for line in self.want_runs}

    def session_status(self, ident, sid):
        """The status of sid's session comment on ident, else None."""
        prefix = f"Run {sid} · "
        return next(
            (
                m[1]
                for a, body in self.comments(ident)
                if a == HARNESS_EMAIL
                and body.startswith(prefix)
                and (m := SESSION.match(body, len(prefix)))
            ),
            None,
        )

    def kind(self, author, body):
        if author == HUMAN:
            return f"human: {body}"
        if author in ROLE_OF:
            if body.startswith(config.TASKS[ROLE_OF[author]].start):
                return "start"
            if found := [k for text, k in self.texts.items() if text in body]:
                return found[0]
        elif author == HARNESS_EMAIL:
            for sid in self.sids():
                prefix = f"Run {sid} · "
                if body.startswith(prefix) and (m := SESSION.match(body, len(prefix))):
                    return f"session {sid} {m[1]}"
            if m := re.fullmatch(r"Promoted to (\S+)\.", body):
                return f"promoted {m[1]}"
            if body in HARNESS_TEXTS:
                return HARNESS_TEXTS[body]
        return f"unrecognized: {body.splitlines()[0] if body else ''}"

    def verify(self):
        found = []
        for ident, want in self.want.items():
            actual = {
                "state": self.state(ident),
                "title": self.issue(ident)["title"],
                "history": self.history(ident),
                "comments": [(a, self.kind(a, b)) for a, b in self.comments(ident)],
                "attachments": self.attachments(ident),
                "subscribers": self.subscribers(ident),
                "ledger": self.ledger(ident),
            }
            found += [
                f"{ident} {item} was {value!r}, want {getattr(want, item)!r}"
                for item, value in actual.items()
                if value != getattr(want, item)
            ]
        lines = [tuple(d.get(k) for k in RUN_KEYS) for d in self.runs()]
        overall = {
            "runs.jsonl": (lines, self.want_runs),
            "problems": (self.problems(), self.want_problems),
        }
        found += [
            f"{item} was {value!r}, want {want!r}"
            for item, (value, want) in overall.items()
            if value != want
        ]
        if found:
            self.fail("; ".join(found))

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

    def launch(self, ident, role, script, resumed):
        """router.py --issue ident with script behind a new gate, until its fake_claude waits there
        and run.py's session comment says running; returns the runs.jsonl line, fake_claude's call
        and the events since.
        """
        if self.pending:
            self.fail(
                f"a claim or resume of {ident} while {self.pending.ident}'s run is pending: agent_run() first"
            )
        self.n_gates += 1
        gate = os.path.join(self.root, f"gate-{self.n_gates}")
        self.scene(script.steps, gate=gate)
        calls, events, lines = len(agent_calls(self.calls())), len(self.logged()), len(self.runs())
        res = self.router("--issue", ident)
        if res.returncode != 0:
            self.fail(
                f"router.py exit {res.returncode}: {res.stderr.strip()!r}; events {self.logged()[events:]}"
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
            self.expect(p.ident, comments=comments, ledger={p.sid: steps}, **changes)

    def backlog_issue(self, role, title, description, labels=()):
        ident = self.fake.issue(
            title,
            state="backlog",
            assignee=self.ids[ACCOUNTS[role]],
            project=PROJECT,
            description=description,
            labels=labels,
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
            self.expect(ident, state=to, history=[(self.want[ident].state, to, HUMAN)])

    def human_say(self, ident, text, to, name):
        with self.step(name):
            self.fake.comment(ident, self.ids[HUMAN], text)
            self.fake.move(ident, to, actor=self.ids[HUMAN])
            self.expect(
                ident,
                state=to,
                history=[(self.want[ident].state, to, HUMAN)],
                comments=[(HUMAN, f"human: {text}")],
            )

    def handoff(self, ident, text):
        self.human_say(ident, text, "handoff", "human: handoff")

    def claim(self, ident, role, script, task=None):
        with self.step("router: claim"):
            line, call, events = self.launch(ident, role, script, False)
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
                ident, state="in_progress", history=[("todo", "in_progress", HARNESS_EMAIL)]
            )
            self.expect_session(ident, sid, "running")
            self.expect_problem(FIRST_WRITE)
        return sid

    def resume(self, ident, role, sid, script):
        with self.step("router: resume"):
            line, call, _ = self.launch(ident, role, script, True)
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
                state="done",
                history=[("handoff", "done", HARNESS_EMAIL)],
                comments=[(HARNESS_EMAIL, f"promoted {child}")],
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
            f"{router.attempt_count(entries, ident, moved)} attempts since the human's move to Todo at "
            f"{moved.isoformat()}; runs.jsonl at {[e.ts.isoformat() for e in entries]}"
        )

    def prompt(self, ident, sid):
        """The input of sid's first run: run.jsonl's input record after sid's first session record."""
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
                state="done",
                assignee=self.ids[ACCOUNTS["researcher"]],
                project=PROJECT,
                description=RESEARCH[1],
            )
            path = f"{RESEARCH_DIR}{datetime.now(UTC):%Y-%m-%d}-{src['identifier']}-session-registries.md"
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
