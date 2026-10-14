"""Human resume commands preserve exec environment names through the shell."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from unittest import mock

from agent_pm.utils import probe, resume_command

from utils.tests.case import Case
from utils.tests.sandbox import clean_env


class ResumeEnvironment(Case):
    def test_exec_names_and_shell_metacharacters_reach_the_child_unchanged(self):
        values = {"A-B": "a b", "X;Y": "$(false); 'x'", "-x": "", "A B": "v"}
        command = resume_command(
            os.getcwd(),
            values,
            (
                sys.executable,
                "-I",
                "-c",
                "import json, os, sys; print(json.dumps({k: os.environ[k] for k in sys.argv[1:]}))",
                *values,
            ),
        )
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=clean_env(),
        )
        self.assertEqual(json.loads(result.stdout), values)


WAIT = 10
PROBE_TIMEOUT = 2
SLEEPER = "import time; time.sleep(60)"


def python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", textwrap.dedent(code))


def kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def gone(pid: int) -> bool:
    """Whether pid has exited: no such process, or a zombie its new parent has not reaped yet."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = subprocess.run(
        ("ps", "-o", "stat=", "-p", str(pid)), capture_output=True, text=True
    ).stdout
    return stat.strip().startswith("Z")


def until(check) -> bool:
    """Whether check() holds within WAIT s."""
    deadline = time.monotonic() + WAIT
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class Probe(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def probe(self, argv, timeout=WAIT, env=None):
        """probe(argv) in self.dir, the Popen call's mock and the processes it made."""
        made, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        with mock.patch("subprocess.Popen", side_effect=popen) as spy:
            out = probe(argv, cwd=self.dir, env={**os.environ, **(env or {})}, timeout=timeout)
        return out, spy, made

    def test_its_stdout_alone_run_in_cwd_with_env_leading_its_own_group(self):
        out, spy, _ = self.probe(
            python("""
            import os, sys
            print(os.getcwd())
            print(os.environ["HM_PROBE"])
            print(os.getpgrp() == os.getpid())
            sys.stderr.write("noise\\n")
        """),
            env={"HM_PROBE": "probe"},
        )
        self.assertEqual(out, f"{os.path.realpath(self.dir)}\nprobe\nTrue\n")
        self.assertIs(spy.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(spy.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_a_spawn_error_is_none(self):
        for argv in ((os.path.join(self.dir, "missing"),), (sys.executable, "-c", "pass\0")):
            with self.subTest(argv=argv):
                self.assertIsNone(self.probe(argv)[0])

    def test_after_timeout_its_group_is_killed_it_is_reaped_and_the_result_none(self):
        pids = os.path.join(self.dir, "grandchild.json")
        out, _, made = self.probe(
            python(f"""
            import json, os, subprocess, sys, time
            grandchild = subprocess.Popen([sys.executable, "-I", "-c", {SLEEPER!r}])
            with open({pids!r} + ".tmp", "w") as f:
                json.dump(grandchild.pid, f)
            os.replace({pids!r} + ".tmp", {pids!r})
            print("partial", flush=True)
            time.sleep(60)
        """),
            timeout=PROBE_TIMEOUT,
        )
        with open(pids, encoding="utf-8") as f:
            grandchild = json.load(f)
        self.addCleanup(kill, grandchild)
        self.assertIsNone(out)
        self.assertEqual([proc.returncode for proc in made], [-signal.SIGKILL])
        self.assertTrue(until(lambda: gone(grandchild)), "grandchild still runs")
