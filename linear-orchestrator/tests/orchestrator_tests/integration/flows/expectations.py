import contextlib
import re

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import SERVICE_ERRORS
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.fixture_errors import StepFailure
from orchestrator_tests.integration.flows.fixtures import (
    HARNESS_EMAIL,
    HUMAN,
    TmuxFlow,
)
from orchestrator_tests.integration.flows.types import (
    HARNESS_TEXTS,
    ROLE_OF,
    RUN_KEYS,
    SESSION,
    BoardChanges,
    Want,
)


class BoardExpectations(TmuxFlow):
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
            raise StepFailure(n, name, e) from e
        except (*SERVICE_ERRORS, DriverError, SessionConfigError) as e:
            e.add_note(f"failed at step {n} ({name})")
            raise

    def track(self, ident, **fields):
        rec = self.issue(ident)
        self.want[ident] = Want(
            **{"state": fake_linear.NAMES[rec["state"]], "title": rec["title"], **fields}
        )

    def expect(self, ident, changes: BoardChanges):
        want = self.want[ident]
        want.state = want.state if changes.state is None else changes.state
        want.title = want.title if changes.title is None else changes.title
        want.history += changes.history
        want.comments += changes.comments
        want.attachments += changes.attachments
        want.subscribers += changes.subscribers
        want.ledger.update(changes.ledger or {})

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
        elif author == HARNESS_EMAIL and (kind := self.harness_kind(body)):
            return kind
        return f"unrecognized: {body.splitlines()[0] if body else ''}"

    def harness_kind(self, body):
        for sid in self.sids():
            prefix = f"Run {sid} · "
            if body.startswith(prefix) and (m := SESSION.match(body, len(prefix))):
                return f"session {sid} {m[1]}"
        if m := re.fullmatch(r"Promoted to (\S+)\.", body):
            return f"promoted {m[1]}"
        if body in HARNESS_TEXTS:
            return HARNESS_TEXTS[body]
        return None

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
