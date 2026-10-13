"""Popen results for tmux protocol fixtures."""

from unittest import mock


def process(stdout="", stderr="", returncode=0):
    child = mock.MagicMock()
    child.__enter__.return_value = child
    child.communicate.return_value = (stdout, stderr)
    child.poll.return_value = child.returncode = returncode
    return child
