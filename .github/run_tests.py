"""Run suites in a supervised child, annotate failures and optionally record durations."""

import collections
import contextlib
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from pathlib import Path

import isolation

from utils.tests.sandbox import clean_env

STATE = "RUN_TESTS_STATE"
TAIL = 40
GATE = None


def escape(text, *, prop=False):
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotate(title, message):
    print(f"::error title={escape(title, prop=True)}::{escape(message)}", flush=True)


def append(variable, text):
    path = os.environ.get(variable)
    if path:
        with (
            GATE.reporting(path) if GATE else contextlib.nullcontext(),
            open(path, "a", encoding="utf-8") as file,
        ):
            file.write(text)


class AnnotatingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = []

    def startTest(self, test):
        self.guard = GATE.test(test) if GATE else None
        if self.guard:
            self.guard.__enter__()
        self.started = time.monotonic()
        append(STATE, f"start {test.id()}\n")
        super().startTest(test)

    def stopTest(self, test):
        if GATE:
            for error in GATE.violations:
                self.addError(test, (type(error), error, error.__traceback__))
            GATE.violations.clear()
        append(
            "RUN_TESTS_DURATIONS",
            json.dumps({"test": test.id(), "seconds": time.monotonic() - self.started}) + "\n",
        )
        super().stopTest(test)
        if self.guard:
            self.guard.__exit__(None, None, None)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.annotate(test, err)

    def addError(self, test, err):
        super().addError(test, err)
        self.annotate(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.annotate(subtest, err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.report(test.id(), "unexpected success")

    def annotate(self, test, err):
        kind, value = err[0].__name__, str(err[1]).strip()
        self.report(test.id(), f"{kind}: {value.splitlines()[0]}" if value else kind)

    def report(self, test_id, line):
        self.failed.append((test_id, line))
        annotate(test_id, line)


def run(suite, title):
    result = unittest.TextTestRunner(verbosity=2, resultclass=AnnotatingResult).run(suite)
    if GATE:
        for error in GATE.violations:
            result.report(title, str(error))
        GATE.violations.clear()
    successful = result.wasSuccessful() and not result.failed
    if result.failed:
        cells = [(test, line.replace("|", "\\|")) for test, line in result.failed]
        rows = [f"| `{test}` | {line} |" for test, line in cells]
        table = "\n".join(["| Test | Error |", "| --- | --- |", *rows])
        append("GITHUB_STEP_SUMMARY", f"### {title}: {len(rows)} failed\n\n{table}\n\n")
    append(STATE, "passed\n" if successful else "failed\n")
    return 0 if successful else 1


def crash(code, lines):
    started = [line[6:] for line in lines if line.startswith("start ")]
    last = started[-1] if started else None
    if lines[-1:] == ["passed"]:
        return (
            f"interpreter crashed after all tests passed (exit {code}); last test started: {last}"
        )
    if lines[-1:] == ["failed"]:
        return f"interpreter crashed after test failures (exit {code}); last test started: {last}"
    if last:
        return f"interpreter crashed (exit {code}) in {last}"
    return f"exited {code} before any test started"


def stack(lines):
    """faulthandler's Python stack (without the C stack 3.14 adds), else the last TAIL lines."""
    fatal = [i for i, line in enumerate(lines) if "Fatal Python error" in line]
    if not fatal:
        return lines[-TAIL:]
    block = lines[fatal[-1] :]
    end = next((i for i, line in enumerate(block) if "C stack trace" in line), len(block))
    return block[:end]


def supervise(title):
    """None in the child; in the parent, the child's exit code (128 + signal for a signal)."""
    if STATE in os.environ:
        return None
    tail = collections.deque(maxlen=5 * TAIL)
    repo = Path(__file__).resolve().parents[1]
    with (
        tempfile.TemporaryDirectory() as tmp,
        tempfile.TemporaryDirectory(prefix=".test-sandbox-", dir=repo) as workspace,
    ):
        state = os.path.join(tmp, "state")
        open(state, "w").close()
        roots = [
            repo,
            repo / ".github",
            repo / "linear-orchestrator/tests",
            *sorted(repo.glob("*/src")),
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
        ]
        env = clean_env(
            {**os.environ, "HOME": workspace, "AGENT_PM_TEST_WORKSPACE": workspace}, *roots
        )
        env.update(
            HOME=workspace,
            TMPDIR=tmp,
            TMUX_TMPDIR=tmp,
            TUI_ATTACH_PREFIX="",
            AGENT_PM_TEST_SANDBOX=tmp,
            AGENT_PM_TEST_TMP=workspace,
            AGENT_PM_TEST_WORKSPACE=workspace,
        )
        isolation.blockers(Path(tmp) / "blocked-bin")
        try:
            with subprocess.Popen(
                [sys.executable, "-P", "-S", "-X", "faulthandler", *sys.orig_argv[1:]],
                env={**env, STATE: state},
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            ) as child:
                for line in child.stderr:
                    sys.stderr.write(line)
                    tail.append(line)
        finally:
            socket = Path(tmp) / f"tmux-{os.getuid()}" / "default"
            if socket.exists():
                subprocess.run(
                    ["tmux", "-S", str(socket), "kill-server"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
        with open(state, encoding="utf-8") as file:
            lines = file.read().splitlines()
    code = 128 - child.returncode if child.returncode < 0 else child.returncode
    if code and not (code == 1 and lines[-1:] == ["failed"]):
        message = crash(code, lines)
        annotate(title, message + "\n" + "".join(stack(list(tail))))
        append("GITHUB_STEP_SUMMARY", f"### {title}\n\n{message}\n\n")
    return code


def discovery_top(root, start):
    root = root.resolve()
    start = Path(start).resolve()
    if start == root / ".github":
        return start
    for parent in [start, *start.parents]:
        if (
            parent.parent.name == "tests"
            and parent.name not in {"unit", "integration"}
            and (parent / "__init__.py").is_file()
        ):
            return parent.parent
    return root


def discover(root, directories):
    suite = unittest.TestSuite()
    for directory in directories:
        start = root / directory
        loader = unittest.TestLoader()
        suite.addTests(
            loader.discover(
                str(start), pattern="*_test.py", top_level_dir=str(discovery_top(root, start))
            )
        )
        if loader.errors:
            raise ValueError("\n".join(loader.errors))
    return suite


def unit_directories(root):
    return [
        "tests/unit",
        *sorted(
            str(path.relative_to(root))
            for pattern in ("*/tests/unit", "*/tests/*/unit")
            for path in root.glob(pattern)
            if (path / "__init__.py").is_file()
        ),
        ".github",
    ]


def main(argv):
    global GATE
    if len(argv) < 2:
        sys.exit("usage: run_tests.py <title> <start dir>... | <title> --unit")
    title, *directories = argv
    code = supervise(title)
    if code is not None:
        return code
    root = Path.cwd()
    GATE = isolation.install(root)
    GATE.unit_only = directories == ["--unit"] or all(
        "unit" in Path(path).parts or path == ".github" for path in directories
    )
    GATE.unit = directories == ["--unit"] or any(
        "unit" in Path(path).parts or path == ".github" for path in directories
    )
    return run(
        discover(root, unit_directories(root) if directories == ["--unit"] else directories), title
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
