"""Shared native runtime fixtures for adapter integration tests."""

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver
from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    SessionRequest,
    Spec,
    TurnEnd,
)

from session.tests.case import REPO, Case, settings
from session.tests.integration.adapters.codex import fake_codex

NATIVE = fake_codex.NATIVE

CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class HeadlessSessions(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work, self.bin = (self.root / name for name in ("home", "work", "bin"))
        self.cwd, self.extra = self.work / "project", self.root / "extra"
        self.tasks = self.root / "team" / "tasks"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.tasks, self.codex_home, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        executable = self.bin / "codex"
        executable.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(fake_codex.__file__)
            + ' "$@"\n'
        )
        executable.chmod(0o755)
        self.log, self.scenario = self.root / "exec.jsonl", self.root / "scenario.json"
        self.scene()
        self.adapter = CodexAdapter(
            {
                "tiers": {"1": "gpt-test"},
                "efforts": {"low": "low"},
            },
        )
        self.common = self.adapter.common
        self.sid = "caller-id"
        self.spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(
                cwd=str(self.cwd),
                project=True,
                dirs=(str(self.extra),),
                reads=(str(self.tasks),),
                commands=("git status",),
            ),
        )
        cfg = self.adapter.config | {
            "env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)}
        }
        self.config = settings({"codex": {"implementation": "adapters.codex:CodexAdapter", **cfg}})
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "CODEX_HOME": str(self.codex_home),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                },
            )
        )
        self.driver = Driver(**self.config)

    def scene(self, steps=None, **fields):
        if steps is not None:
            fields["steps"] = steps
        self.scenario.write_text(json.dumps(fields))

    def drain(self, handle, on=lambda _event: None):
        self.addCleanup(handle.stop)
        found = []

        def consume():
            for event in handle.events():
                found.append(event)
                on(event)

        executor = ThreadPoolExecutor(max_workers=1)
        result = executor.submit(consume)
        try:
            result.result(timeout=10)
        except TimeoutError:
            handle.stop()
            self.fail("Driver did not reach Exited within 10s")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return found

    def logged(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def written(self):
        return records.read(records.path(str(self.work)))

    def record(self):
        return next(Path(store_root()).glob("*.json"))

    def expected_args(self, prompt, *, resume=False, sandbox="danger-full-access"):
        args = [
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-s",
            sandbox,
            "-c",
            'approval_policy="on-request"',
            "-c",
            'approvals_reviewer="auto_review"',
        ]
        if sandbox == "workspace-write":
            args.extend(
                [
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.writable_roots=[]",
                ]
            )
        args.extend(["-m", "gpt-test", "-c", "model_reasoning_effort=low"])
        args.extend(["--add-dir", str(self.extra)])
        return [
            *args,
            *(["resume", NATIVE] if resume else []),
            "--",
            prompt + "\n\n" + self.adapter.runtime_note,
        ]

    def direct_launch(self, prompt, *, resume=False):
        adapter = CodexAdapter(
            {
                key: value
                for key, value in self.config["adapters"]["codex"].items()
                if key != "implementation"
            },
        )
        run = adapter.headless()
        launch = run.launch(
            prompt, SessionRequest(dataclasses.replace(self.spec, sid=self.sid), resumed=resume)
        )
        env = {key: value for key, value in os.environ.items() if key not in launch.unset}
        result = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env={**env, **launch.env, "PWD": launch.cwd},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(run.events(result.stdout.splitlines()))[-1], TurnEnd("three"))
        entry = self.logged()[-1]
        expected = self.expected_args(prompt, resume=resume)[:-1] + [prompt]
        self.assertEqual(entry["argv"], expected)
        self.assertEqual(entry["prompt"], prompt)
        self.assertEqual(entry["resume_id"], NATIVE if resume else None)
