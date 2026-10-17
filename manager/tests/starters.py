import subprocess
from unittest import mock


def finished(returncode=0, stderr=""):
    proc = mock.MagicMock(returncode=returncode)
    proc.__enter__.return_value = proc
    proc.communicate.return_value = (None, stderr)
    return proc


def answered(run, *starts):
    """Popen side effect: `starts` (else every argv) answer as a subprocess.run fake."""
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
