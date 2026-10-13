"""CI test runner: `run_tests.py <title> <start dir>` runs the *_test.py under <start dir>, top level the
cwd, in a -X faulthandler child. Each failure or error prints a GitHub ::error annotation titled with the
test id and a row in $GITHUB_STEP_SUMMARY; the parent annotates a crash (a nonzero exit the results do
not explain) with the last test started and the stderr tail. integration_jobs.py runs jobs through it."""

import collections
import os
import subprocess
import sys
import tempfile
import unittest

STATE = "RUN_TESTS_STATE"
TAIL = 40


def escape(text, prop=False):
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotate(title, message):
    print(f"::error title={escape(title, prop=True)}::{escape(message)}", flush=True)


def append(variable, text):
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


class AnnotatingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = []

    def startTest(self, test):
        append(STATE, f"start {test.id()}\n")
        super().startTest(test)

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
        self.report(test, "unexpected success")

    def annotate(self, test, err):
        kind, value = err[0].__name__, str(err[1]).strip()
        self.report(test, f"{kind}: {value.splitlines()[0]}" if value else kind)

    def report(self, test, line):
        self.failed.append((test.id(), line))
        annotate(test.id(), line)


def run(suite, title):
    result = unittest.TextTestRunner(verbosity=2, resultclass=AnnotatingResult).run(suite)
    if result.failed:
        cells = [(test, line.replace("|", "\\|")) for test, line in result.failed]
        rows = [f"| `{test}` | {line} |" for test, line in cells]
        table = "\n".join(["| Test | Error |", "| --- | --- |", *rows])
        append("GITHUB_STEP_SUMMARY", f"### {title}: {len(rows)} failed\n\n{table}\n\n")
    append(STATE, "passed\n" if result.wasSuccessful() else "failed\n")
    return 0 if result.wasSuccessful() else 1


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
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "state")
        open(state, "w").close()
        with subprocess.Popen(
            [sys.executable, "-X", "faulthandler", *sys.orig_argv[1:]],
            env={**os.environ, STATE: state},
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        ) as child:
            for line in child.stderr:
                sys.stderr.write(line)
                tail.append(line)
        with open(state, encoding="utf-8") as file:
            lines = file.read().splitlines()
    code = 128 - child.returncode if child.returncode < 0 else child.returncode
    if code and not (code == 1 and lines[-1:] == ["failed"]):
        message = crash(code, lines)
        annotate(title, message + "\n" + "".join(stack(list(tail))))
        append("GITHUB_STEP_SUMMARY", f"### {title}\n\n{message}\n\n")
    return code


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: run_tests.py <title> <start dir>")
    title, start = argv
    code = supervise(title)
    if code is not None:
        return code
    suite = unittest.TestLoader().discover(start, pattern="*_test.py", top_level_dir=os.getcwd())
    return run(suite, title)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
