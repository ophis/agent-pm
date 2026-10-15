"""A separately packaged Delegate runs and recovers through configuration in fresh processes."""

import json
import os
import subprocess
from pathlib import Path

from agent_pm.delegate import Layout
from agent_pm.utils import Record, Status
from orchestrator_tests.case import PY, Case, child_env
from orchestrator_tests.config_fixtures import PIPELINE, ConfigFile
from orchestrator_tests.linear_run_case import SID

from delegate.tests.fakes import delegate as fake_delegate

EXTENSION = """
class ExtensionDelegate(FakeDelegate):
    ROLES = {
        "researcher": {"deep-research": "research"},
        "pm": {"product-design": "design"},
        "engineer": {"build": "build"},
    }

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls._known(role)
        return RunConfig(
            role=role, task=task or "", tier=2, effort="high", client="fake",
            output={"type": "github", "repo": "acme/notes", "branch": "main", "dir": role + "/"},
            idle_timeout=60,
            show="",
        )

class NewDefault(ExtensionDelegate):
    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        raise AssertionError("saved provider was replaced by new default")
"""


class Extension(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        package = Path(self.dir) / "external_delegate"
        package.mkdir()
        source = Path(fake_delegate.__file__).read_text(encoding="utf-8")
        (package / "__init__.py").write_text(source + EXTENSION, encoding="utf-8")
        self.env = child_env(os.environ, self.dir)
        self.configure()

    def configure(self, default="extension"):
        pipeline = "\n".join(
            line for line in PIPELINE.splitlines() if not line.startswith("delegates = ")
        )
        self.local(
            f"work_dir = {json.dumps(self.dir + '/state')}\n"
            + pipeline
            + f'\n[delegates]\ndefault = "{default}"\n'
            + '[delegates.extension]\nimplementation = "external_delegate:ExtensionDelegate"\n'
            + '[delegates.new]\nimplementation = "external_delegate:NewDefault"\n'
        )

    def child(self, code, *, stdin=""):
        result = subprocess.run(
            [*PY, "-c", code],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.dir,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def start(self, issue):
        return Record.parse(
            self.child(
                "from agent_pm.linear_orchestrator.engine import config, runs\n"
                "from agent_pm.linear_orchestrator import LinearRun\n"
                "cfg = config.load()\n"
                f"owner = LinearRun({issue!r}, 'Echo.', cfg.run_dir({issue!r}), cfg=cfg, role='engineer', sid={SID!r}, display='opaque display')\n"
                f"runs.note(cfg.runs_log, 'start', issue={issue!r}, sid={SID!r}, role='engineer', client='fake', delegate_kind=cfg.delegate_kind)\n"
                "print(owner.detach().to_line())\n"
            )
        )

    def test_separate_provider_creates_and_resumes_saved_kind_after_default_changes(self):
        record = self.start("TASK-1")
        self.assertEqual(
            (record.kind, record.name, record.display), ("pipeline", "TASK-1", "opaque display")
        )
        self.configure(default="new")
        output = self.child(
            "from agent_pm.linear_orchestrator import LinearRun\n"
            "from agent_pm.linear_orchestrator.engine import config\n"
            "view = LinearRun.attach('TASK-1', cfg=config.load())\n"
            "print(view.resume().to_line())\n"
        )
        self.assertEqual(Record.parse(output), record)
        state = Path(Layout(record.workdir).run) / "delegate.json"
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["starts"], 1)
        for transition, starts in (("finish", 1), ("end", 2)):
            output = self.child(
                "from external_delegate import ExtensionDelegate\n"
                "from agent_pm.linear_orchestrator import cli\n"
                f"ExtensionDelegate.{transition}({record.workdir!r})\n"
                "raise SystemExit(cli.main(['resume', 'TASK-1']))\n"
            )
            self.assertEqual(Record.parse(output), record)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["starts"], starts)

    def test_cli_batch_status_and_resume_run_in_another_process(self):
        first, second = self.start("TASK-1"), self.start("TASK-2")
        for args, stdin in (
            (["status", "TASK-2", "TASK-1"], ""),
            (["status"], second.to_line() + "\n" + first.to_line() + "\n"),
        ):
            output = self.child(
                f"from agent_pm.linear_orchestrator import cli\nraise SystemExit(cli.main({args!r}))\n",
                stdin=stdin,
            )
            statuses = [Status.parse(line) for line in output.splitlines()]
            self.assertEqual([item.name for item in statuses], ["TASK-2", "TASK-1"])
            self.assertTrue(all(item.alive for item in statuses))
        output = self.child(
            "from agent_pm.linear_orchestrator import cli\nraise SystemExit(cli.main(['resume', 'TASK-1']))\n"
        )
        self.assertEqual(Record.parse(output), first)
