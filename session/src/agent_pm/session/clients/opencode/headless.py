"""OpenCode's headless Mode: `opencode run --format json` behind launcher.py, which feeds it the
prompt on stdin (`run` quotes an argument holding a space). The first sessionID binds the run; text
parts are Replies, tools Activity, an error the failed turn; the end is the turn's, its last text.
"""

import json
import os
import sys

from agent_pm.session.clients.opencode import launcher
from agent_pm.session.clients.opencode.common import (
    NATIVE,
    PERMISSIONS,
    RESERVED,
    UNSET,
    OpenCodeCommon,
    refuse,
)
from agent_pm.session.engine.types import (
    Activity,
    ClientBound,
    ConfigError,
    Launch,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
)
from agent_pm.session.modes.client_adapters.headless import Headless

SESSION_TIMEOUT = 120  # seconds a run may take to print its first sessionID


class OpenCodeHeadless(Headless):
    def __init__(self, common: OpenCodeCommon):
        self.common = common
        self.cwd, self.sid = "", ""
        self.bound, self.failed, self.last = False, False, ""

    def launch(self, spec: Spec):
        context, common = self._context, self.common
        if context.bin_dir is not None:
            raise ConfigError("the opencode client runs on a local Host only")
        refuse(spec.flags, (*RESERVED, *PERMISSIONS), "a run's flags")
        cwd, sid = spec.access.cwd, spec.sid or ""
        selection: tuple[str, ...] = ()
        if spec.resume:
            native = common.native(cwd, sid)
            if native is None:
                raise ConfigError(f"no OpenCode session bound to run ID {sid!r}")
            selection = common.selector(native, resume=True)
        model, variant = common.model(spec.tier), common.variant(spec.effort)
        command = (
            "opencode",
            "run",
            "--format",
            "json",
            *selection,
            *(("--model", model) if model is not None else ()),
            *(("--variant", variant) if variant is not None else ()),
            *common.flags,
            *common.modes[context.permission],
            *spec.flags,
        )
        self.cwd, self.sid = cwd, sid
        self.bound, self.failed, self.last = False, False, ""
        return Launch(
            (
                os.path.realpath(sys.executable),
                "-I",
                launcher.__file__,
                "--timeout",
                str(SESSION_TIMEOUT),
                "--prompt",
                spec.prompt,
                "--",
                *command,
            ),
            env=common.environment(spec, context),
            unset=UNSET,
            cwd=cwd,
            project=spec.access.project,
        )

    def events(self, lines):
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                yield Stderr("opencode: invalid JSON event")
                continue
            if isinstance(record, dict):
                yield from self.parse(record)
        if not self.bound:
            yield TurnFailed("no_session", "opencode ended before it started a session")
        elif not self.failed:
            yield TurnEnd(self.last)

    def parse(self, record: dict):
        if not self.bound and isinstance(native := record.get("sessionID"), str):
            if NATIVE.fullmatch(native):
                self.common.bind(self.cwd, self.sid, native)
                self.bound = True
                yield ClientBound(native)
            else:
                yield Stderr(f"opencode: invalid sessionID {native!r:.80}")
        kind, part = record.get("type"), record.get("part")
        part = part if isinstance(part, dict) else {}
        if kind == "text":
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                self.last = value
                yield Reply(value)
            elif not isinstance(value, str):
                yield Activity(kind)
        elif kind == "tool_use":
            tool = part.get("tool")
            yield Activity(tool if isinstance(tool, str) else kind)
            state = part.get("state")
            if isinstance(state, dict) and state.get("status") == "error":
                yield Stderr(f"opencode: {tool}: {state.get('error')}")
        elif kind == "error":
            self.failed = True
            error = record.get("error")
            error = error if isinstance(error, dict) else {}
            data = error.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            name = error.get("name")
            yield TurnFailed(
                name if isinstance(name, str) else "error",
                message if isinstance(message, str) else "",
            )
        elif isinstance(kind, str):
            yield Activity(kind)
