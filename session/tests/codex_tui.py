"""Shared Codex launch plans and protection fixtures."""

import dataclasses
import json
import os
import sys
from unittest import mock

from agent_pm.session.adapters.codex.common.types import Snapshot
from agent_pm.session.adapters.codex.tui import HookPlan, LaunchPlan

from session.tests.case import SRC, Case
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.adapters.codex.support import Base

ROOTS_JSON = json.dumps([SRC["session"], SRC["utils"]])
HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "/protected/session", ROOTS_JSON)


class ProtectCase(Base):
    def launch(self, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            return (
                self.adapter.tui()
                .launch(
                    dataclasses.replace(spec, prompt="prompt", resume=resume),
                    HOOK,
                    source_roots=PACKAGE_ROOTS,
                )
                .argv[-1]
            )


class PlanCase(Case):
    def setUp(self):
        self.snapshot = Snapshot(
            cwd="/work",
            project=True,
            dirs=("/granted",),
            commands=(),
            model="configured",
            effort=None,
            codex_home="/runtime/codex",
            flags=(
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "--",
                "--config=literal",
                "--disable",
                "literal",
            ),
            env={},
        )
        self.plan = LaunchPlan(
            executable="/protected/codex",
            selection=("resume", "saved-native"),
            prompt="prompt\nwith Unicode: 字",
            hook=HookPlan(
                (os.path.realpath(sys.executable), "-I", "/protected/mode hook.py", "/session dir"),
                (SRC["session"], SRC["utils"]),
            ),
            workdir="/work",
            sid="caller-id",
            store="/private-state/recovery/codex",
            protect=("/protected/events",),
        )
        self.frontend = (
            "/protected/codex",
            "resume",
            "saved-native",
            "--no-daemon",
            "-C",
            "/work",
            "-c",
            'tui.notification_method="bel"',
        )
