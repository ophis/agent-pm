"""Guards for subprocess commands forbidden in a test."""

import subprocess
from unittest import mock


class UnexpectedCommand(AssertionError):
    def __init__(self, command):
        super().__init__(f"unexpected command: {command}")


def reject_command(command):
    run = subprocess.run

    def checked(argv, *args, **kwargs):
        if argv[0] == command:
            raise UnexpectedCommand(command)
        return run(argv, *args, **kwargs)

    return mock.patch("subprocess.run", checked)
