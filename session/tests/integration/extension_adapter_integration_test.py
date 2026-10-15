"""A new client is one importable module plus one [adapters.<name>] table naming it as
module.path:Class: a temp copy of the session and utils packages, the fake adapter's package copied in outside
them as module extension_client (a name unrelated to the client's) and its table appended to the
copy's agent_pm/session/config.toml, nothing else changed, runs a headless session through Session.start in a
child Python whose PYTHONPATH is only the copy's site directory and that module's dir. A table without its
module, or a module without its table, fails there before anything starts.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from session.tests import fake_adapter
from session.tests.case import PY, REPO, Case, child_env

TIMEOUT = 60  # seconds
SKIP = shutil.ignore_patterns("__pycache__")
PROJECTS = ("session", "utils")
MODULE = "extension_client"
# The child: configures the session from the copy's session config, starts client argv[2] headless
# in workdir argv[1] and prints its events; a ConfigError exits 1, its text on stderr.
CHILD = """import dataclasses, json, sys
from agent_pm.session import Access, ConfigError, Mode, Session, Spec, read_config
from agent_pm.session.engine import hosted_session
try:
    Session.configure_from(read_config())
    handle = Session.start(
        Spec(workdir=sys.argv[1], access=Access(cwd=sys.argv[1]), client=sys.argv[2],
             mode=Mode.HEADLESS, prompt="hello")
    )
except ConfigError as e:
    sys.exit(f"ConfigError: {e}")
events = [[type(e).__name__, dataclasses.asdict(e)] for e in handle.events()]
print(json.dumps({"sid": handle.sid, "events": events,
                  "files": [hosted_session.__file__, sys.modules["extension_client"].__file__]}))
"""
INSTALLED_CHILD = """import json, sys
from unittest import mock
from agent_pm.session import Access, Spec, read_config
from agent_pm.session.engine.hosted_session import HostedSession
HostedSession.configure_from(read_config())
adapter = HostedSession._config().registry.adapter("codex")
spec = Spec(workdir=sys.argv[1], sid="installed-layout", access=Access(cwd=sys.argv[1], project=True))
with mock.patch("shutil.which", return_value="/protected/codex"):
    launch = adapter.tui().launch(spec, (sys.executable, "-I", "/protected/hook.py"))
print(json.dumps({"argv": launch.argv[1:-1], "pythonpath": launch.env.get("PYTHONPATH"),
                  "plan": json.loads(launch.argv[-1])}))
"""


class ExtensionAdapterIntegration(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.site = os.path.join(self.root, "site")
        self.src = os.path.join(self.site, "agent_pm", "session")
        self.extensions = os.path.join(self.root, "extensions")
        self.package(MODULE)
        self.package("untabled")
        for project in PROJECTS:
            shutil.copytree(
                os.path.join(REPO, project, "src", "agent_pm", project),
                os.path.join(self.site, "agent_pm", project),
                ignore=SKIP,
                dirs_exist_ok=True,
            )
        self.srcs = os.pathsep.join((self.site, self.extensions))
        self.config = os.path.join(self.src, "config.toml")
        os.chmod(self.config, 0o600)
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home)
        self.temp = os.path.join(self.root, "tmp")
        os.mkdir(self.temp)
        self.work = os.path.join(self.root, "work")
        scenario = os.path.join(self.root, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"out": {"type": "session", "id": "native-1"}},
                    {"out": {"type": "message", "text": "hi"}},
                    {"out": {"type": "result", "text": "done"}},
                ],
                f,
            )
        self.table(
            f'[adapters.fake]\nimplementation = "{MODULE}:FakeAdapter"\n'
            f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(os.path.join(self.root, 'log.jsonl'))}\n"
        )

    def package(self, name: str) -> None:
        """The fake adapter's package copied in as top-level package <name>, outside session."""
        shutil.copytree(
            os.path.dirname(fake_adapter.__file__),
            os.path.join(self.extensions, name),
            ignore=SKIP,
        )

    def table(self, text: str) -> None:
        with open(self.config, "a", encoding="utf-8") as f:
            f.write("\n" + text)

    def child(self, client: str, script: str = CHILD) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*PY, "-c", script, self.work, client, self.site],
            cwd=self.root,
            env={
                **child_env({**os.environ, "HOME": self.home, "TMPDIR": self.temp}),
                "PYTHONPATH": self.srcs,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def refused(self, client: str, error: str) -> None:
        """The child starting `client` exits 1 on ConfigError `error`, before any run record or
        process.
        """
        child = self.child(client)
        self.assertEqual((child.returncode, child.stdout), (1, ""), child.stderr)
        self.assertIn(f"ConfigError: {error}", child.stderr)
        self.assertFalse(os.path.exists(self.work))

    def test_a_copied_adapter_package_and_its_table_run_a_headless_session(self):
        child = self.child("fake")
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        hosted, module = data["files"]
        self.assertTrue(hosted.startswith(self.src + os.sep), hosted)
        self.assertTrue(module.startswith(os.path.join(self.extensions, MODULE) + os.sep), module)
        self.assertEqual([name for name, _ in data["events"]], ["Reply", "TurnEnd", "Exited"])
        self.assertEqual(data["events"][-1], ["Exited", {"returncode": 0}])
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as f:
            written = [json.loads(line) for line in f]
        self.assertEqual(
            [(e["kind"], e["sid"]) for e in written],
            [("session", data["sid"]), ("client_session", data["sid"])],
        )

    def test_installed_codex_starts_its_module_with_its_site_as_the_only_import_root(self):
        child = self.child("codex", INSTALLED_CHILD)
        self.assertEqual(child.returncode, 0, child.stderr)
        launched = json.loads(child.stdout)
        self.assertEqual(
            launched["argv"], ["-P", "-s", "-m", "agent_pm.session.adapters.codex.tui.start"]
        )
        self.assertEqual(launched["pythonpath"], self.site)
        self.assertNotIn("protect", launched["plan"])

    def test_a_table_without_its_module_fails_before_launch(self):
        self.table('[adapters.absent]\nimplementation = "absent_client:AbsentAdapter"\n')
        self.refused(
            "absent",
            "[adapters.absent].implementation: absent_client:AbsentAdapter: cannot import "
            "absent_client: No module named 'absent_client'",
        )

    def test_a_module_without_its_table_fails_before_launch(self):
        self.assertTrue(os.path.isdir(os.path.join(self.extensions, "untabled")))
        self.refused("untabled", "no [adapters.untabled] table")


if __name__ == "__main__":
    unittest.main()
