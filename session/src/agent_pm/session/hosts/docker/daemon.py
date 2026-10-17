"""The account's Docker Desktop daemon: its fixed endpoint, and the one docker command line that
reaches it whatever DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG say.
"""

import os
import pwd

CLEARED = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


def account_home() -> str:
    """The system account's home, never $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def endpoint() -> str:
    return f"unix://{account_home()}/.docker/run/docker.sock"


def docker_call(docker: str, *args: str) -> tuple[list[str], dict[str, str]]:
    """argv and env for `docker <args>` against endpoint()."""
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    return [docker, "--host", endpoint(), *args], env
