"""FakeDelegate, a Delegate with one session that runs nothing: its state is
<workdir>/delegate.json (0600), so a view from attach and the Delegate that wrote it agree.
end(workdir) and finish(workdir) stand for its sessions ending, without and with an outcome."""

import json
import os
import uuid
from dataclasses import asdict

from agent_pm.delegate import (
    Begun,
    ConfigError,
    Delegate,
    Finished,
    Outcome,
    Progress,
    Result,
)
from agent_pm.delegate.composer import RunConfig
from agent_pm.session import NotDisplayable, SessionStatus
from agent_pm.utils import Record, Status, files

FILE = "delegate.json"


def _load(workdir) -> dict:
    try:
        with open(os.path.join(workdir, FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        raise ConfigError(f"{workdir!r}: no {FILE}") from None


def _save(workdir: str, state: dict) -> dict:
    files.save(os.path.join(workdir, FILE), json.dumps(state))
    return state


def _update(workdir: str, **changes) -> dict:
    return _save(workdir, {**_load(workdir), **changes})


class FakeDelegate(Delegate):
    ROLES = {"dummy": {"echo": "echoes its input"}}
    outcome_result = Outcome("done", "Echoed", "echoed its input", deliverable="hello")
    usage_result = None

    def __init__(
        self,
        role,
        spec,
        *,
        driver=None,
        session_cls=None,
        composer=None,
        display=None,
        host=None,
        harness_host=None,
        callback=None,
    ) -> None:
        self._known(role)
        sid = spec.sid or str(uuid.uuid4())
        self.workdir, self.input = os.path.abspath(spec.workdir), spec.input
        name = spec.name or f"{role}-{sid[:8]}"
        state = {"role": role, "sid": sid, "name": name, "display": display}
        _save(self.workdir, {**state, "outer": {}, "alive": False, "outcome": None, "starts": 0})

    @classmethod
    def _known(cls, role: str) -> None:
        if role not in cls.ROLES:
            raise ConfigError(f"no role {role!r}")

    @classmethod
    def attach(cls, record_or_workdir):
        workdir = getattr(record_or_workdir, "workdir", record_or_workdir)
        _load(workdir)
        view = cls.__new__(cls)
        view.workdir = os.path.abspath(workdir)
        return view

    @classmethod
    def usage(cls, role):
        cls._known(role)
        return cls.usage_result

    @classmethod
    def roles(cls):
        return {role: dict(tasks) for role, tasks in cls.ROLES.items()}

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls._known(role)
        return RunConfig(
            role=role,
            task=task or "",
            tier=2,
            effort="high",
            runtime="fake",
            output={"type": "local"},
        )

    @classmethod
    def trusted_dirs(cls):
        return frozenset()

    @classmethod
    def end(cls, workdir: str) -> None:
        _update(workdir, alive=False, outcome=None)

    @classmethod
    def finish(cls, workdir: str) -> None:
        _update(workdir, alive=False, outcome=asdict(cls.outcome_result))

    def _record(self, state: dict) -> Record:
        outer = {
            "kind": state["role"],
            "ctl": ["agent-pm-delegate"],
            "resume": ["agent-pm-delegate", "resume", self.workdir],
            **state["outer"],
        }
        name = state["name"]
        return Record(
            1,
            outer["kind"],
            name,
            (name,),
            state["sid"],
            self.workdir,
            tuple(outer["ctl"]),
            tuple(outer["resume"]),
            state["display"],
            state["role"],
        )

    def detach(self, *, outer=None):
        return self._record(_update(self.workdir, outer=dict(outer or {}), alive=True, starts=1))

    def run(self, on_event=None):
        emit = on_event or (lambda event: None)
        result = Result(0, self.outcome_result)
        emit(Begun(_load(self.workdir)["sid"]))
        emit(Progress("start", self.input))
        _update(self.workdir, outcome=asdict(result.outcome))
        emit(Finished(result))
        return result

    def alive(self):
        return _load(self.workdir)["alive"]

    def status(self):
        state = _load(self.workdir)
        outcome = state["outcome"] and state["outcome"]["status"]
        word = "working" if state["alive"] else "gone"
        return Status(state["name"], state["alive"], word, outcome, None)

    def outcome(self):
        found = _load(self.workdir)["outcome"]
        return None if found is None else Outcome(**found)

    def show(self):
        raise NotDisplayable("host fake cannot display")

    def attach_command(self):
        raise NotDisplayable("host fake cannot display")

    def close(self):
        _update(self.workdir, alive=False)

    def session_info(self):
        return SessionStatus("yes", None, "")

    def resume(self):
        state = _load(self.workdir)
        if not (state["alive"] or state["outcome"]):
            state = _update(self.workdir, alive=True, starts=state["starts"] + 1)
        return self._record(state)
