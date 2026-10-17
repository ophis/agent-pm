"""Fake starter processes for tests that answer a Crew start or resume, which Crew waits on through
subprocess.Popen.
"""

import subprocess
from unittest import mock


def finished(returncode=0, stderr=""):
    """A Popen whose process has exited `returncode`, printing `stderr`."""
    proc = mock.MagicMock(returncode=returncode)
    proc.__enter__.return_value = proc
    proc.communicate.return_value = (None, stderr)
    return proc


def answered(run, *starts):
    """A subprocess.Popen side effect: each process (with `starts`, only of those argv; any other
    runs) answers, when waited on, as `run` (a subprocess.run fake, given the wait's timeout) does.
    What `run` itself starts runs.
    """
    real = subprocess.Popen

    def popen(argv, **options):
        if starts and tuple(argv) not in starts:
            return real(argv, **options)
        proc = finished()

        def communicate(timeout):
            with mock.patch("subprocess.Popen", real):
                result = run(argv, timeout=timeout, **options)
            proc.returncode = result.returncode
            return None, result.stderr

        proc.communicate.side_effect = communicate
        return proc

    return popen
