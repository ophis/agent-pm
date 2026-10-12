"""The Composer in a child process: PY with PYTHONPATH this tree's import root, the test's own HOME (its
~/.agent-pm/core-new.local.toml the only local config) and a temp cwd, printing one JSON value. COMPOSE composes one
agent run as its caller (the Delegate) would."""

from new.tests import case
from new.tests.case import PY, child_env

import json
import os
import subprocess
import tempfile

TIMEOUT = 60
PRELUDE = "import json, os, sys\nfrom agent_pm import composer\n"
COMPOSE = """import dataclasses
req = json.loads(sys.argv[1])
try:
    team = composer.team(composer.TEAM, composer.team_dirs())
    run = composer.resolve(req["role"], req.get("task"), overlays=req.get("overlays", ()), team=team)
    intent = composer.access(run, req["workdir"], cwd=req["cwd"], project=False, team=team)
    own = team.tasks_dir(run.role)
    writes = [("the workdir", req["workdir"]), ("the cwd", req["cwd"])]
    writes += [("a dir the run adds", d) for d in intent.dirs
               if not own or os.path.realpath(d) != os.path.realpath(own)]
    composer.check_team_dirs(team.dirs, writes=writes)
    params = composer.RunParams(input=req["input"], out=req["out"], workdir=req["workdir"],
                                resume=req.get("resume", False))
    print(json.dumps({"run": dataclasses.asdict(run), "intent": dataclasses.asdict(intent),
                      "prompt": composer.render(run, params, team=team), "trusted": sorted(composer.trusted_dirs()),
                      "files": [composer.__file__, sys.modules["agent_pm.utils.config_file"].__file__,
                                sys.modules["agent_pm.utils.paths"].__file__],
                      "repo_imported": "agent_pm.repo" in sys.modules}))
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
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), mode, encoding="utf-8") as f:
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
