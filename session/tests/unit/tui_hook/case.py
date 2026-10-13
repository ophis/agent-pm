"""Fixtures for tui hook tests."""

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

from session.tests.case import SRC, Case

HOOK = os.path.join(SRC["session"], "agent_pm", "session", "modes", "tui_hook.py")

PANE = "%3"

LINE = r"\A[0-9]{2}:[0-9]{2}:[0-9]{2} w1 %s\n\Z"  # one event line

TS = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z"

WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}

MISSING = object()

RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)

TMUX = """#!{python} -I
import json, os, sys
with open(os.environ["TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("TMUX_FAIL"):
    sys.exit("boom\\nagain")
if sys.argv[1] == "display-message":
    print("w1")
"""

DECOY = """#!{python} -I
open({log!r}, "a").close()
"""


def install(folder: str, text: str) -> str:
    os.mkdir(folder)
    path = os.path.join(folder, "tmux")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def state(root: str, events) -> dict:
    return {
        "runtime": "fake",
        "sid": str(uuid.uuid4()),
        "workdir": root,
        "cwd": root,
        "project": False,
        "transcript": "",
        "resume": "",
        "events": events,
        "words": dict(WORDS),
        "tmux_path": os.path.join(root, "bin", "tmux"),
    }


def write_state(folder: str, value) -> None:
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    fd = os.open(os.path.join(folder, "session.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class HookBase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        self.events = os.path.join(self.root, "events")
        os.close(os.open(self.events, os.O_WRONLY | os.O_CREAT, 0o600))
        self.tmux = install(os.path.join(self.root, "bin"), TMUX.format(python=sys.executable))
        self.decoy = os.path.join(self.root, "decoy.log")
        install(
            os.path.join(self.root, "path"), DECOY.format(python=sys.executable, log=self.decoy)
        )
        self.log = os.path.join(self.root, "tmux.log")
        self.env = {
            "PATH": os.path.join(self.root, "path"),
            "TMUX_PANE": PANE,
            "TMUX_LOG": self.log,
            "TMUX_TMPDIR": self.root,
            "HOME": self.root,
        }
        self.state = state(self.root, self.events)
        write_state(self.dir, self.state)

    def hook(self, label, payload=None, args=None, **env):
        """Runs the hook (env: changes to self.env, None drops a key); checks it exits 0 printing
        nothing on stdout; returns its stderr.
        """
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps({} if payload is None else payload).encode()
        )
        environ = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        res = subprocess.run(
            [sys.executable, "-I", HOOK, *([self.dir, label] if args is None else args)],
            input=data,
            capture_output=True,
            env=environ,
            timeout=60,
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()

    def assertError(self, err, text):
        """err is one stderr line holding text."""
        self.assertRegex(err, r"\Atui_hook: [^\n]*\n\Z")
        self.assertIn(text, err)

    def assertNothingWritten(self):
        self.assertFalse(os.path.lexists(self.hooks))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.lines(), "")

    def records(self):
        with open(self.hooks, "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(lines[-1], b"")
        return [json.loads(line) for line in lines[:-1]]

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def lines(self):
        with open(self.events) as f:
            return f.read()

    def reset(self):
        open(self.events, "w").close()
        if os.path.exists(self.log):
            os.unlink(self.log)
