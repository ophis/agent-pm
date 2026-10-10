"""Segment, a Flow whose test is numbered steps: each step helper below is one `with self.step(name):` that applies
its expected changes to the snapshot (track, expect, expect_session, expect_runs, expect_problem), which verify()
checks against the fake Linear, runs.jsonl, the ledgers and the failed requests as the step ends."""
import contextlib
import copy
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import hermetic  # noqa: F401  (first: config reads HOME at import)
import config
import attended
import drive
import linear
import manager
import router
import sessions
from flow_fixtures import (ACCOUNTS, HARNESS_EMAIL, HUMAN, MANAGER, PRD_DIR, PROJECT, RESEARCH_DIR, TIMEOUT, Flow,
                           fake_linear, live_tmux)

ROLE_OF = {a: r for r, a in ACCOUNTS.items()}
DIRS = {"researcher": RESEARCH_DIR, "pm": PRD_DIR}
TEXT_KINDS = {"done": "done", "needs_input": "question", "failed": "failed"}
RUN_STEPS = {"done": "run: in review", "needs_input": "run: needs input", "failed": "run: failed",
             "none": "run: no outcome", "give_up": "run: gave up"}
STOP = {"kind": "hook", "event": "Stop"}
SESSION = re.compile(r"(running|done|interrupted) · ")
HARNESS_TEXTS = {router.CAP_COMMENT: "cap", router.INTERRUPTED: "interrupted"}
RESEARCH = ("Session registries in agent harnesses", "How do agent harnesses keep track of their sessions?")
INSTRUCTIONS = "Write the PRD for a session registry."
REPORT = "# Session registries in agent harnesses\n\nEach harness names a tmux session after its run."
BUILD = "Build the session registry."


@dataclass
class Want:
    """A tracked issue's expected snapshot; comments are (author, kind) pairs (Segment.kind)."""
    state: str
    title: str
    history: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)


class Script(NamedTuple):
    """An agent run's fake claude script (Segment.script): outcome, one of RUN_STEPS; steps, its first turn's; turns,
    the later turns'; texts, {summary or question text: comment kind}."""
    outcome: str
    steps: list
    turns: list
    texts: dict

    @property
    def report(self):
        """The outcome report's fields; None without one."""
        return next((s["outcome"] for s in self.steps if s["kind"] == "outcome"), None)


class Pending(NamedTuple):
    """A claimed or resumed agent run parked at its gate; tui, an attended run's TUI session, else None."""
    ident: str
    role: str
    sid: str
    script: Script
    gate: str
    resumed: bool
    tui: str | None


class Segment(Flow):
    """Flow plus steps, the snapshot and the step helpers. Roles are researcher and pm; a role's account is
    ACCOUNTS[role]; the human acts through the fake as HUMAN."""

    def setUp(self):
        super().setUp()
        self.want, self.want_runs, self.want_problems, self.texts, self.tuis = {}, [], [], {}, {}
        self.n_steps = self.n_scripts = self.n_gates = 0
        self.pending = None

    # Steps and the snapshot

    @contextlib.contextmanager
    def step(self, name):
        """Step n (1, 2, … per test); verify() on a normal exit. An AssertionError (a timeout's too) is re-raised as
        `failed at step <n> (<name>): <message>`; any other exception gets that as a note."""
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
        """Adds ident to the snapshot: Want's fields, state and title the fake's unless given."""
        with self.fake.lock:
            rec = self.fake.find(ident)
            fields = {"state": fake_linear.NAMES[rec["state"]], "title": rec["title"], **fields}
        self.want[ident] = Want(**fields)

    def expect(self, ident, *, state=None, title=None, history=(), comments=(), attachments=(), subscribers=(),
               ledger=None):
        """A step's expected changes of ident: state and title replace, the lists append, ledger sets each sid's."""
        want = self.want[ident]
        want.state = want.state if state is None else state
        want.title = want.title if title is None else title
        want.history += history
        want.comments += comments
        want.attachments += attachments
        want.subscribers += subscribers
        want.ledger.update(ledger or {})

    def expect_session(self, ident, sid, status):
        """sid's session comment on ident is now `status`: updated in place, else added."""
        comments, entry = self.want[ident].comments, (HARNESS_EMAIL, f"session {sid} {status}")
        at = next((i for i, (a, k) in enumerate(comments) if a == HARNESS_EMAIL and k.startswith(f"session {sid} ")),
                  None)
        if at is None:
            comments.append(entry)
        else:
            comments[at] = entry

    def expect_runs(self, *lines):
        """runs.jsonl lines added, (kind, issue, sid, role) each."""
        self.want_runs += lines

    def expect_problem(self, *problems):
        """Injected failures answered, (op, account, result) each (Flow.problems)."""
        self.want_problems += problems

    def sids(self):
        return {line[2] for line in self.want_runs}

    def kind(self, author, body):
        """A comment's kind (`human: <body>`, start, done, question, failed, `session <sid> <status>`,
        `promoted <ID>`, cap, interrupted), else `unrecognized: <first line>`."""
        if author == HUMAN:
            return f"human: {body}"
        if author in ROLE_OF:
            if body.startswith(config.TASKS[ROLE_OF[author]].start):
                return "start"
            if found := [k for text, k in self.texts.items() if text in body]:
                return found[0]
        elif author == HARNESS_EMAIL:
            for sid in self.sids():
                if body.startswith(sessions.prefix(sid)) and (m := SESSION.match(body, len(sessions.prefix(sid)))):
                    return f"session {sid} {m[1]}"
            if m := re.fullmatch(r"Promoted to (\S+)\.", body):
                return f"promoted {m[1]}"
            if body in HARNESS_TEXTS:
                return HARNESS_TEXTS[body]
        return f"unrecognized: {body.splitlines()[0] if body else ''}"

    def verify(self):
        """Fails, every mismatch named in a fixed order, unless each tracked issue and runs.jsonl and the problems
        are as expected."""
        found = []
        for ident, want in self.want.items():
            with self.fake.lock:
                title = self.fake.find(ident)["title"]
            actual = {"state": self.state(ident), "title": title, "history": self.history(ident),
                      "comments": [(a, self.kind(a, b)) for a, b in self.comments(ident)],
                      "attachments": self.attachments(ident), "subscribers": self.subscribers(ident),
                      "ledger": self.ledger(ident)}
            found += [f"{ident} {item} was {value!r}, want {getattr(want, item)!r}"
                      for item, value in actual.items() if value != getattr(want, item)]
        runs = [(d["kind"], d["issue"], d["sid"], d["role"]) for d in self.runs()]
        overall = {"runs.jsonl": (runs, self.want_runs), "problems": (self.problems(), self.want_problems)}
        found += [f"{item} was {value!r}, want {want!r}" for item, (value, want) in overall.items() if value != want]
        if found:
            self.fail("; ".join(found))

    # Run scripts

    def script(self, role, outcome, ident, url=None):
        """The Script of the next agent run of ident (runs counted per test: its texts and title carry the ordinal):
        a start mark, then the outcome's report; none and give_up report nothing. give_up's later turn (the nudge's)
        stops drive.STOP_LIMIT - 1 times; its end's stop makes the limit. A done report's url is `url`, else a new
        report of the role's docs dir."""
        self.n_scripts += 1
        n = self.n_scripts
        steps = [{"kind": "progress", "name": "start", "text": f"Starting run #{n}."}]
        if outcome in ("none", "give_up"):
            return Script(outcome, steps, [[STOP] * (drive.STOP_LIMIT - 1)] if outcome == "give_up" else [], {})
        report = {"status": outcome, "title": f"Session registry run {n}", "summary": f"Summary #{n}."}
        if outcome == "done":
            report["url"] = url or self.doc_url(f"{DIRS[role]}{datetime.now(timezone.utc):%Y-%m-%d}-{ident}-run-{n}.md")
        if outcome == "needs_input":
            report["questions"] = [f"Question #{n}?"]
        text = report["questions"][0] if outcome == "needs_input" else report["summary"]
        return Script(outcome, [*steps, {"kind": "outcome", "outcome": report}], [], {text: TEXT_KINDS[outcome]})

    def launch(self, ident, role, script, resumed, tui=False, managed=False, entry=None):
        """router.py --issue ident, script's scene held at a fresh gate; tui: attended (--tui --split-from MANAGER, plus
        --manager MANAGER when managed), a control-mode client showing MANAGER (attached once); entry: a name in
        MANAGER's roster whose recovery command runs instead (run_recovery). Exit 0, one runs.jsonl line added; waits
        for the fake claude's log line, by when run.py has closed the TUI session a tui run of ident left open (one
        tui-closed event, that session gone), else logged no tui- event. Registers the texts, makes the run pending.
        Returns (that line, the fake's argv, the events logged meanwhile)."""
        if self.pending:
            self.fail(f"a claim or resume of {ident} while {self.pending.ident}'s run is pending: agent_run() first")
        if tui and not self.server.clients:
            self.server.attach(MANAGER)
        self.n_gates += 1
        gate = os.path.join(self.server.root, f"gate-{self.n_gates}")
        self.scene(steps=script.steps, turns=script.turns, gate=gate)
        calls, events, runs = len(self.calls()), len(self.logged()), len(self.runs())
        if entry is not None:
            res = self.run_recovery(entry)
        else:
            flags = ("--tui", "--split-from", MANAGER, *(("--manager", MANAGER) if managed else ())) if tui else ()
            res = self.router("--issue", ident, *flags)
        if res.returncode != 0:
            self.fail(f"router.py exit {res.returncode}: {res.stderr.strip()!r}; events {self.logged()[events:]}")
        added = self.runs()[runs:]
        self.assertEqual(len(added), 1, f"runs.jsonl lines added: {added}")
        line = added[0]

        def started():
            try:
                return self.calls()[calls:]
            except ValueError:   # a line half appended
                return None
        call = live_tmux.wait(started, TIMEOUT, "the fake claude's start")[0]
        logged, left = self.logged()[events:], self.tuis.pop(ident, None)
        self.assertEqual([(e["src"], e["kind"], e.get("session")) for e in logged
                          if e.get("issue") == ident and e["kind"].startswith("tui-")],
                         [("run", "tui-closed", left)] if left else [])
        if left:
            self.assertFalse(self.live(left), f"{left} still live")
        self.texts.update(script.texts)
        self.pending = Pending(ident, role, line["sid"], script, gate, resumed,
                               f"{attended.prefix(role, ident)}-{line['sid'][:8]}" if tui else None)
        return line, call["argv"], logged

    def agent_run(self):
        """Step `run: …` (RUN_STEPS) of the pending run: opens its gate, waits for its end; a tui run's TUI session is
        left with its claude running (launch checks the next run closes it). Then expects its write-back
        (config.TASKS): a session comment done; a start comment and ledger step once per sid; an outcome's comment,
        title, attachment, subscriber and move."""
        p = self.pending
        if p is None:
            self.fail("agent_run() without a pending claim or resume")
        with self.step(RUN_STEPS[p.script.outcome]):
            with open(p.gate, "w"):
                pass
            self.wait_end(p.sid, p.ident, sum(line[2] == p.sid for line in self.want_runs), p.role)
            self.pending = None
            if p.tui:
                shown = self.server.tmux("display-message", "-p", "-t", f"={p.tui}:", "#{pane_dead}")
                self.assertEqual(shown.stdout, "0\n", f"TUI session {p.tui} not running: exit {shown.returncode}, "
                                                      f"{shown.stderr.strip()!r}")
                self.tuis[p.ident] = p.tui
            want, task, account, report = self.want[p.ident], config.TASKS[p.role], ACCOUNTS[p.role], p.script.report
            self.expect_session(p.ident, p.sid, "done")
            first = "start" not in want.ledger.get(p.sid, [])
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
            self.expect(p.ident, comments=comments, ledger={p.sid: steps}, **changes)

    def roster(self):
        """MANAGER's roster.json entries (core/src/manager.py), each without its `started`."""
        with open(os.path.join(self.apm, "managers", MANAGER, "roster.json")) as f:
            entries = json.load(f)["entries"]
        return {n: {k: v for k, v in e.items() if k != "started"} for n, e in entries.items()}

    def run_recovery(self, name):
        """Roster entry `name`'s recovery command (manager.recovery), no shell, in its cwd, as its manager runs it: from
        MANAGER's pane (env() plus that pane's TMUX and TMUX_PANE)."""
        e = self.roster()[name]
        return subprocess.run(manager.recovery(e), cwd=e["cwd"], env={**self.env(), **self.server.inside(MANAGER)},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)

    # Step helpers

    def backlog_issue(self, role, title, description, labels=()):
        """A Backlog issue of the role's account in PROJECT, tracked; no step of its own. Returns its ID."""
        ident = self.fake.issue(title, state="backlog", assignee=self.ids[ACCOUNTS[role]], project=PROJECT,
                                description=description, labels=labels)["identifier"]
        self.track(ident, state="backlog", title=title)
        return ident

    def seed_backlog(self, role, title, description, labels=()):
        """Step `seed: backlog`: backlog_issue. Returns its ID."""
        with self.step("seed: backlog"):
            ident = self.backlog_issue(role, title, description, labels)
        return ident

    def refused(self, ident, note=""):
        """router.py --issue ident exits 1, else fails naming its stderr, the events it logged and note; returns those
        events."""
        events = len(self.logged())
        res = self.router("--issue", ident)
        added = self.logged()[events:]
        if res.returncode != 1:
            self.fail("; ".join([f"router.py exit {res.returncode}: {res.stderr.strip()!r}", f"events {added}",
                                 *([note] if note else [])]))
        return added

    def backlog_not_claimed(self, ident):
        """Step `router: backlog not claimed`: refused, its one event a pick-none."""
        with self.step("router: backlog not claimed"):
            self.assertEqual([(e["src"], e["kind"], e.get("issue")) for e in self.refused(ident)],
                             [("router", "pick-none", ident)])

    def human_move(self, ident, to, name):
        """Step `name`: the human moves ident to `to`."""
        with self.step(name):
            self.fake.move(ident, to, actor=self.ids[HUMAN])
            self.expect(ident, state=to, history=[(self.want[ident].state, to, HUMAN)])

    def human_say(self, ident, text, to, name):
        """Step `name`: the human comments `text` on ident, then moves it to `to`."""
        with self.step(name):
            self.fake.comment(ident, self.ids[HUMAN], text)
            self.fake.move(ident, to, actor=self.ids[HUMAN])
            self.expect(ident, state=to, history=[(self.want[ident].state, to, HUMAN)],
                        comments=[(HUMAN, f"human: {text}")])

    def handoff(self, ident, text):
        """Step `human: handoff`: human_say to Handoff."""
        self.human_say(ident, text, "handoff", "human: handoff")

    def kill_tui(self, ident):
        """Step `human: kill the TUI session` agent_run() left running of ident's attended run, its driver session
        already gone: the run has no session left, so the next launch's run.py closes none."""
        with self.step("human: kill the TUI session"):
            tui = self.tuis.pop(ident)
            self.assertEqual(self.server.tmux("kill-session", "-t", f"={tui}").returncode, 0)
            self.assertFalse(self.live(tui))

    def claim(self, ident, role, script, task=None, tui=False, managed=False):
        """Step `router: claim` of the Todo issue ident with script (agent_run() runs it): a new sid's start line, the
        claim event's task `task`, no transient event; In Progress by the harness; its session comment running.
        tui, managed: attended (launch). Returns the sid."""
        with self.step("router: claim"):
            line, argv, events = self.launch(ident, role, script, False, tui, managed)
            sid = line["sid"]
            self.assertEqual((line["kind"], line["issue"], line["role"]), ("start", ident, role))
            self.assertNotIn(sid, self.sids())
            self.assertIn("--session-id", argv)
            self.assertEqual(argv[argv.index("--session-id") + 1], sid)
            mine = [e for e in events if e.get("issue") == ident]
            self.assertEqual([e for e in mine if e["kind"] == "transient"], [])
            self.assertEqual([(e["role"], e.get("task")) for e in mine if e["kind"] == "claim"], [(role, task)])
            self.expect_runs(("start", ident, sid, role))
            self.expect(ident, state="in_progress", history=[("todo", "in_progress", HARNESS_EMAIL)])
            self.expect_session(ident, sid, "running")
        return sid

    def resume(self, ident, role, sid, script, entry=None):
        """Step `router: resume` of sid with script (agent_run() runs it), headless: a resume line, the fake's argv
        `--resume <sid>`, no `--session-id`; sid's session comment running again. entry: step `manager: recover`, the
        recovery command of MANAGER's roster entry `entry` (launch) instead, attended. Returns sid."""
        with self.step("router: resume" if entry is None else "manager: recover"):
            line, argv, _ = self.launch(ident, role, script, True, tui=entry is not None, entry=entry)
            self.assertEqual((line["kind"], line["issue"], line["sid"], line["role"]), ("resume", ident, sid, role))
            self.assertNotIn("--session-id", argv)
            self.assertIn("--resume", argv)
            self.assertEqual(argv[argv.index("--resume") + 1], sid)
            self.expect_runs(("resume", ident, sid, role))
            self.expect_session(ident, sid, "running")
        return sid

    def promote_now(self):
        """promote.py --now as a process (Flow.promote, which the step promote() overrides)."""
        return Flow.promote(self, "--now")

    def promote(self, ident, role, nxt):
        """Step `promote` of ident, role's issue in Handoff: promote.py --now exits 0, printing nothing; ident Done by
        the harness, its comment naming the child; the child as assert_child says, tracked. Returns the child's ID."""
        with self.step("promote"):
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))
            child, title = self.assert_child(ident, role, nxt)
            self.expect(ident, state="done", history=[("handoff", "done", HARNESS_EMAIL)],
                        comments=[(HARNESS_EMAIL, f"promoted {child}")])
            self.track(child, state="todo", title=title)
        return child

    def promote_nothing(self, ident):
        """Step `promote: nothing`: promote.py --now exits 0, printing nothing; no promote.M_CREATE request yet;
        nothing changes; ident never reached Handoff."""
        with self.step("promote: nothing"):
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))
            self.assertEqual(self.sent("promote.M_CREATE"), [])
            self.assertNotIn("handoff", [to for _, to, _ in self.history(ident)])

    def assert_child(self, ident, role, nxt):
        """ident's one `related` child is nxt's account's, in Todo, in ident's project, titled from ident and
        described from it (handoff line, Source = its attachments, Instructions = the human's comments after its last
        move to In Review); every promote.M_CREATE the harness's, one per child. Returns (child ID, title)."""
        with self.fake.lock:
            src = copy.deepcopy(self.fake.find(ident))
            related = [b for t, a, b in self.fake.relations if t == "related" and a == src["id"]]
            self.assertEqual(len(related), 1, f"issues related from {ident}: {related}")
            child = copy.deepcopy(self.fake.find(related[0]))
            name = self.fake.users[self.ids[HUMAN]]["name"]
        prefix, want = config.TASKS[role].prefix, self.want[ident]
        title = f"{config.TASKS[nxt].prefix}: " + (want.title.removeprefix(f"{prefix}: ") if prefix else want.title)
        self.assertEqual((child["assignee"], fake_linear.NAMES[child["state"]], child["project"], child["title"]),
                         (self.ids[ACCOUNTS[nxt]], "todo", src["project"], title))
        cutoff = max(h["createdAt"] for h in src["history"] if fake_linear.NAMES[h["toStateId"]] == "in_review")
        said = [f"{name}, {c['createdAt']}:\n{c['body']}" for c in src["comments"]
                if c["user"] == self.ids[HUMAN] and c["createdAt"] > cutoff]
        head = [f"Handoff from {ident}: {src['url']}",
                *(["## Source\n" + "\n".join(f"- {a['title']}: {a['url']}" for a in want.attachments)]
                  if want.attachments else []),
                *(["## Instructions\n" + "\n\n".join(said)] if said else [])]
        self.assertEqual(child["description"].partition("\n\n## Comments\n")[0], "\n\n".join(head))
        made = [(r.account, r.variables["in"]["id"]) for r in self.requests() if r.op == "promote.M_CREATE"]
        self.assertEqual({a for a, _ in made}, {HARNESS_EMAIL})
        ids = [i for _, i in made]
        self.assertIn(child["id"], ids)
        self.assertEqual(len(ids), len(set(ids)), f"promote.M_CREATE made an issue twice: {ids}")
        return child["identifier"], title

    def assert_pm_start(self, ident):
        """ident is the pm account's, in Todo, in PROJECT, tracked."""
        self.assertIn(ident, self.want)
        with self.fake.lock:
            rec = copy.deepcopy(self.fake.find(ident))
        self.assertEqual((rec["assignee"], fake_linear.NAMES[rec["state"]], rec["project"]),
                         (self.ids[ACCOUNTS["pm"]], "todo", PROJECT))

    def moved_to_todo(self, ident):
        """The time of the human's last move of ident to Todo."""
        with self.fake.lock:
            return linear.last_move(self.fake.find(ident)["history"], {fake_linear.IDS["todo"]}, {self.ids[HUMAN]})

    def wait_next_second(self, ident):
        """Sleeps into the wall-clock second after moved_to_todo: router.attempt_count counts the runs.jsonl lines
        strictly after that move, and their ts is to the second (drive.stamp), so a claim within its second is no
        attempt."""
        time.sleep(max(0, (self.moved_to_todo(ident).replace(microsecond=0) + timedelta(seconds=1)
                           - datetime.now(timezone.utc)).total_seconds()))

    def attempts(self, ident):
        """router.attempt_count's attempts of ident since moved_to_todo, and the times compared."""
        moved, entries = self.moved_to_todo(ident), router.parse_log(os.path.join(self.logs, "runs.jsonl"))
        return (f"{router.attempt_count(entries, ident, moved)} attempts since the human's move to Todo at "
                f"{moved.isoformat()}; runs.jsonl at {[e[0].isoformat() for e in entries]}")

    def prompt(self, ident, sid):
        """sid's first prompt, the run's input: its transcript's first line, a user line."""
        first = self.transcript(ident, sid)[0]
        self.assertEqual(first["type"], "user")
        return first["message"]["content"]

    def section(self, prompt, heading):
        """prompt's `## <heading>` section body, up to the next `## ` line, a fenced one too: no text a test publishes
        or reports may hold one. Fails without the section."""
        if m := re.search(rf"^## {re.escape(heading)}\n\n(.*?)(?=^## |\Z)", prompt, re.M | re.S):
            return m[1]
        self.fail(f"no `## {heading}` section in the prompt:\n{prompt}")

    # Segment helpers

    def researcher_ready(self, labels=()):
        """Steps 1-3: a researcher Backlog issue (RESEARCH), not claimed, moved to Todo by the human. Returns its ID."""
        ident = self.seed_backlog("researcher", *RESEARCH, labels=labels)
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        return ident

    def researcher_done(self, ident, task=None):
        """Of the Todo research issue ident: a claimed run done, the human's handoff (INSTRUCTIONS), promote. Returns
        the pm child's ID."""
        self.claim(ident, "researcher", self.script("researcher", "done", ident), task=task)
        self.agent_run()
        self.handoff(ident, INSTRUCTIONS)
        return self.promote(ident, "researcher", "pm")

    def researcher_to_pm(self, labels=(), task=None):
        """researcher_ready, then researcher_done. Returns the pm child's ID."""
        return self.researcher_done(self.researcher_ready(labels), task)

    def pm_ready(self):
        """Steps 1-3: `seed: backlog` seeds a Done research issue (RESEARCH) with its report (REPORT) published and
        attached, and a pm Backlog issue handed off from it in promote's form (Source the report, Instructions
        INSTRUCTIONS), both tracked; the pm issue is not claimed, then moved to Todo by the human. Returns its ID."""
        with self.step("seed: backlog"):
            src = self.fake.issue(RESEARCH[0], state="done", assignee=self.ids[ACCOUNTS["researcher"]],
                                  project=PROJECT, description=RESEARCH[1])
            path = f"{RESEARCH_DIR}{datetime.now(timezone.utc):%Y-%m-%d}-{src['identifier']}-session-registries.md"
            report = {"url": self.doc_url(path), "title": RESEARCH[0]}
            self.publish(path, REPORT)
            with self.fake.lock:
                src["attachments"].append(dict(report))
                name = self.fake.users[self.ids[HUMAN]]["name"]
            self.track(src["identifier"], attachments=[report])
            ident = self.backlog_issue("pm", f"{config.TASKS['pm'].prefix}: {RESEARCH[0]}", "\n\n".join([
                f"Handoff from {src['identifier']}: {src['url']}", f"## Source\n- {RESEARCH[0]}: {report['url']}",
                f"## Instructions\n{name}, {fake_linear.stamp()}:\n{INSTRUCTIONS}"]))
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        return ident

    def pm_done(self, ident, url=None):
        """Of the Todo pm issue ident: a claimed run done (its report's url `url`, else a new PRD), the human's handoff
        (BUILD), promote. Returns the engineer child's ID."""
        self.claim(ident, "pm", self.script("pm", "done", ident, url))
        self.agent_run()
        self.handoff(ident, BUILD)
        return self.promote(ident, "pm", "engineer")

    def pm_to_engineer(self, start=None):
        """pm_done of start, a pm start (assert_pm_start), else of pm_ready's issue. Returns the engineer child's ID."""
        if start is None:
            return self.pm_done(self.pm_ready())
        self.assert_pm_start(start)
        return self.pm_done(start)
