"""PtyHost's tui hook callback: marks a session's activity while it is the launch the hook was
installed for (its sid and generation).
"""

from agent_pm.session.engine.types import HookCall, HookError, StatusState
from agent_pm.session.hosts.pty import state
from agent_pm.utils import printable_abs


def on_hook(word: StatusState, call: HookCall) -> None:
    folder, generation = call.state.get("folder"), call.state.get("generation")
    if not isinstance(folder, str) or not printable_abs(folder):
        raise HookError("host state: bad folder")
    if not isinstance(generation, str) or not generation:
        raise HookError("host state: bad generation")
    metadata = state.meta(folder).get("metadata")
    if isinstance(metadata, dict) and (metadata.get("sid"), metadata.get("generation")) == (
        call.sid,
        generation,
    ):
        state.mark(folder, word, call.events)
