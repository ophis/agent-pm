"""Interpreter gates for unit logic and sandboxed integration tools."""

import ast
import contextlib
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

DENIED = {
    "subprocess.Popen",
    "os.exec",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.killpg",
    "os.system",
    "socket.connect",
}
GIT_ALLOWED = {
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
}


def allowlist(root):
    path = root / "tests/guards.py"
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text())
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "UNIT_ISOLATION_ALLOWED"
            for target in node.targets
        )
    )


class Gate:
    def __init__(self, root, sandbox, workspace=None):
        self.root = root.resolve()
        self.sandbox = Path(sandbox).resolve()
        self.workspace = Path(workspace).resolve() if workspace else self.sandbox
        self.allowed = allowlist(self.root)
        self.current = "test discovery"
        self.unit = True
        self.exempt = False
        self.reporting_file = None

    def fail(self, message):
        raise RuntimeError(
            f"test isolation: {self.current}: {message}\n{''.join(traceback.format_stack())}"
        )

    def inside(self, path):
        actual = Path(path).resolve()
        return actual.is_relative_to(self.sandbox) or actual.is_relative_to(self.workspace)

    def audit(self, event, args):
        if event in DENIED and self.unit and not self.exempt:
            self.fail(f"forbidden {event}")
        if event == "subprocess.Popen":
            executable, argv, cwd, env = args
            self.command(executable, argv, cwd, os.environ if env is None else env)
        elif event in {"os.exec", "os.posix_spawn"}:
            executable, argv, env = args
            self.command(executable, argv, None, env)
        elif event == "os.system":
            self.fail("shell launch cannot verify private tool arguments")
        elif event == "open" and self.unit:
            path, mode, flags = args
            writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if (
                writes
                and not isinstance(path, int)
                and path != os.devnull
                and not self.inside(path)
                and Path(path).resolve() != self.reporting_file
            ):
                self.fail(f"write outside temporary directory: {path}")

    def command(self, executable, argv, cwd, env):
        name = os.path.basename(os.fsdecode(executable))
        if name == "tmux":
            if isinstance(argv, str):
                self.fail("tmux requires a private socket")
            options = []
            index = 1
            while index < len(argv) and argv[index].startswith("-"):
                option = argv[index]
                options.append(option)
                if option in {"-S", "-L", "-f", "-T"}:
                    index += 1
                    if index < len(argv):
                        options.append(argv[index])
                index += 1
            attached = next(
                (option[2:] for option in options if option.startswith("-S") and len(option) > 2),
                None,
            )
            if attached is not None:
                socket = attached
            elif "-S" in options:
                index = options.index("-S") + 1
                socket = options[index] if index < len(options) else ""
            else:
                directory = env.get("TMUX_TMPDIR", "")
                if not directory or not self.inside(directory):
                    self.fail("tmux requires a private TMUX_TMPDIR or -S socket")
                socket = env.get("TMUX", "").split(",")[0] or os.path.join(
                    directory, f"tmux-{os.getuid()}", "default"
                )
            if not socket or not self.inside(socket):
                self.fail("tmux socket outside test sandbox")
        if name == "git":
            unsafe = sorted(key for key in env if key.startswith("GIT_") and key not in GIT_ALLOWED)
            if (
                unsafe
                or env.get("GIT_CONFIG_GLOBAL") != os.devnull
                or env.get("GIT_CONFIG_NOSYSTEM") != "1"
            ):
                self.fail(f"git requires clean environment: {unsafe}")

    @contextlib.contextmanager
    def reporting(self, path):
        previous = self.reporting_file
        self.reporting_file = Path(path).resolve()
        try:
            yield
        finally:
            self.reporting_file = previous

    @contextlib.contextmanager
    def test(self, test):
        path = Path(inspect.getfile(type(test))).resolve()
        rel = str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)
        previous = self.current, self.unit, self.exempt
        self.current = test.id()
        self.unit = "unit" in path.parts or path.parent == self.root / ".github"
        self.exempt = rel in self.allowed
        path_before = os.environ.get("PATH", os.defpath)
        if self.unit and not self.exempt:
            os.environ["PATH"] = str(self.sandbox / "blocked-bin") + os.pathsep + path_before
        try:
            yield
        finally:
            self.current, self.unit, self.exempt = previous
            os.environ["PATH"] = path_before


def blockers(directory):
    directory.mkdir()
    for name in ("git", "tmux", "claude", "codex"):
        path = directory / name
        path.write_text(f"#!/bin/sh\necho 'test isolation: forbidden {name}' >&2\nexit 126\n")
        path.chmod(0o700)


def install(root):
    sandbox = os.environ["AGENT_PM_TEST_SANDBOX"]
    tempfile.tempdir = sandbox
    sys.dont_write_bytecode = True
    gate = Gate(root, sandbox, os.environ.get("AGENT_PM_TEST_WORKSPACE"))
    sys.addaudithook(gate.audit)
    return gate
