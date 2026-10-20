"""A TmuxHost whose clients get a container context, as on the Docker Host."""

import sys
from dataclasses import replace

from agent_pm.session.engine.types import ClientContext, Container
from agent_pm.session.hosts.tmux import TmuxHost

PATH = f"{__name__}:ContainerTmuxHost"


class ContainerTmuxHost(TmuxHost):
    def _client_context(self) -> ClientContext:
        container = Container("/opt/bin", sys.executable, "/opt/src", "/run")
        return replace(super()._client_context(), container=container)
