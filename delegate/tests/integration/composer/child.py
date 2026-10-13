"""The Composer in a child process: PY with PYTHONPATH this tree's import root, the test's own HOME
(its ~/.agent-pm/config.local.toml the only local config) and a temp cwd, printing one JSON value.
COMPOSE composes one agent run as its caller (the Delegate) would.
"""

import json
import os
import subprocess
import tempfile

from delegate.tests import case
from delegate.tests.case import PY, child_env

TIMEOUT = 60
PRELUDE = "import json, os, sys\nfrom agent_pm.delegate import composer\n"
COMPOSE = """import dataclasses
req = json.loads(sys.argv[1])
try:
    team = composer.team(composer.TEAM, composer.team_dirs())
    run = composer.resolve(req["role"], req.get("task"), overlays=req.get("overlays", ()), team=team)
    intent = composer.access(run, req["workdir"], cwd=req["cwd"], project=False, team=team)
    writes = [("the workdir", req["workdir"]), ("the cwd", req["cwd"])]
    writes += [("a dir the run adds", d) for d in intent.dirs]
    composer.check_team_dirs(team.dirs, writes=writes)
    params = composer.RunParams(input=req["input"], out=req["out"], workdir=req["workdir"],
                                resume=req.get("resume", False))
    print(json.dumps({"run": dataclasses.asdict(run), "intent": dataclasses.asdict(intent),
                      "prompt": composer.render(run, params, team=team), "trusted": sorted(composer.trusted_dirs()),
                      "files": [composer.__file__, sys.modules["agent_pm.utils.config_file"].__file__,
                                sys.modules["agent_pm.utils.paths"].__file__],
                      "repo_imported": any(os.path.dirname(getattr(m, "__file__", None) or "")
                                           == os.path.join(composer.TEAM, "tools")
                                           for m in list(sys.modules.values()))}))
except composer.ConfigError as e:
    print(json.dumps({"error": str(e)}))
"""


class Case(case.Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.work, self.proj = os.path.join(self.root, "work"), os.path.join(self.root, "proj")
        for d in (self.work, self.proj):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")

    def local(self, text: str, mode: str = "w") -> None:
        with open(os.path.join(self.agent_pm, "config.local.toml"), mode, encoding="utf-8") as f:
            f.write(text)

    def child(self, code: str, *args: str):
        res = subprocess.run(
            [*PY, "-c", PRELUDE + code, *args],
            cwd=self.root,
            env=child_env({**os.environ, "HOME": self.home}),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def compose(self, role: str, task: str | None = None, **req) -> dict:
        req = {
            "role": role,
            "task": task,
            "input": "Hello.",
            "out": self.out,
            "workdir": self.work,
            "cwd": self.work,
            **req,
        }
        return self.child(COMPOSE, json.dumps(req))
