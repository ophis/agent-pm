"""OpenCode's headless Mode: `opencode run --format json` behind the launcher (launcher.py), which
feeds it the prompt on stdin, since `run` quotes a message argument holding a space, and stops a
run that starts no session within SESSION_TIMEOUT seconds.
"""

import os
import sys

from agent_pm.session.clients.opencode import launcher
from agent_pm.session.clients.opencode.common import (
    PERMISSIONS,
    RESERVED,
    UNSET,
    OpenCodeCommon,
    refuse,
)
from agent_pm.session.engine.types import ConfigError, Launch, Spec
from agent_pm.session.modes.client_adapters.headless import Headless

SESSION_TIMEOUT = 120  # seconds a run may take to print its first sessionID


class OpenCodeHeadless(Headless):
    def __init__(self, common: OpenCodeCommon):
        self.common = common
        self.cwd, self.sid = "", ""

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
        return iter(())
