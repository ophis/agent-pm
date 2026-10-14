"""Shared Codex launch plans and protection fixtures."""

import contextlib
import dataclasses
import json
import os
import sys
from unittest import mock

from agent_pm.session.adapters.codex.common.types import Snapshot
from agent_pm.session.adapters.codex.tui import HookPlan, LaunchPlan

from session.tests.case import ROOTS, SRC, Case
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.adapters.codex.support import Base, committed_tui

ROOTS_JSON = json.dumps([SRC["session"], SRC["utils"]])
HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "/protected/session", ROOTS_JSON)
INTERPRETER_PATHS = {
    "stdlib": "/interpreter/lib",
    "platstdlib": "/interpreter/lib",
    "purelib": "/interpreter/site-packages",
    "platlib": "/interpreter/plat-site-packages",
}
SITE_PACKAGES = ("/interpreter/site-packages", "/interpreter/extra-site-packages")
INTERPRETER_ROOTS = (
    "/interpreter/lib",
    "/interpreter/site-packages",
    "/interpreter/plat-site-packages",
    "/interpreter/extra-site-packages",
)
PROTECTED_ROOTS = tuple(
    dict.fromkeys(
        [
            *(
                os.path.realpath(os.path.join(SRC[package], "agent_pm", package))
                for package in ("session", "utils")
            ),
            *(os.path.join(root, "agent_pm") for root in ROOTS),
            *INTERPRETER_ROOTS,
        ]
    )
)


@contextlib.contextmanager
def interpreter(paths=INTERPRETER_PATHS, site_packages=SITE_PACKAGES):
    with (
        mock.patch("sysconfig.get_path", side_effect=paths.__getitem__),
        mock.patch("site.getsitepackages", return_value=list(site_packages)),
    ):
        yield


class ProtectCase(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(interpreter())

    def launch(self, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            return committed_tui(
                self.adapter,
                dataclasses.replace(spec, prompt="prompt", resume=resume),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            ).argv[-1]


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
