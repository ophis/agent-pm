"""Tmux session listing and display diagnostics."""

from agent_pm.session.hosts.tmux.transport import (
    ITERM_ID,
    NAME,
    OPENER,
    PANE,
    PROCESS_ID,
    Transport,
    absent,
    display_error,
)
from agent_pm.session.hosts.tmux.types import TuiError

LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}\t#{@process}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


PANES = "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}"


LAST_LINES = 2000


def _own_pane(name: str, dead: str, process: str, panes: dict[str, str]) -> tuple[str | None, str]:
    """The pane to read a session's last lines from, and its pane_dead."""
    if not process:
        return name, dead
    if not PROCESS_ID.fullmatch(process):
        return None, ""
    pane = process.split(" ")[2]
    return (pane, panes[pane]) if pane in panes else (None, "1")


def _printable(value: str) -> str | None:
    return value if value and value.isprintable() else None


class Listing(Transport):
    def listing(self, match=None) -> list[dict] | None:
        """Sessions named NAME that match (compiled; None: all) fullmatches, by name: tags, pane
        state, `shown` (no client: None; else the newest client's host session, "" if none or not
        NAME), a dead pane's last LAST_LINES lines; a bad or empty field None, a row of the wrong
        width dropped. [] when no server runs, None when tmux fails. A session with @process is read
        through the pane its launch recorded, whichever pane is active: dead once that pane is gone,
        and dead None for a bad @process; any other session through its active pane.
        """
        found = self._table(["list-sessions", "-F", LISTING], 9)
        if found is None:
            return None
        rows = sorted(
            f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
        )
        if not rows:
            return []
        clients = self._table(["list-clients", "-F", SHOWN], 3)
        if clients is None:
            return None
        recent = {}
        for _, tty, session in sorted(
            (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
        ):
            recent[session] = tty
        hosts, launched = {}, {}
        if any(f[0] in recent or f[8] for f in rows):
            panes = self._table(["list-panes", "-a", "-F", PANES], 4)
            if panes is None:
                return None
            hosts = {tty: session for dead, tty, session, _ in panes if dead != "1"}
            launched = {pane_id: dead for dead, _, _, pane_id in panes}
        out = []
        for name, dead, sid, client, state, pane, parent, display, process in rows:
            target, dead = _own_pane(name, dead, process, launched)
            tty = recent.get(name)
            host = "" if tty is None else hosts.get(tty, "")
            out.append(
                {
                    "name": name,
                    "sid": _printable(sid),
                    "client": _printable(client),
                    "state": _printable(state),
                    "pane": pane if PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane) else None,
                    "opener": parent if OPENER.fullmatch(parent) else None,
                    "display": display if display and not display_error(display) else None,
                    "dead": {"1": True, "0": False}.get(dead),
                    "shown": None if name not in recent else host if NAME.fullmatch(host) else "",
                    "last_lines": self._last_lines(target) if dead == "1" and target else None,
                }
            )
        return out

    def _table(self, args: list[str], width: int) -> list[list[str]] | None:
        try:
            # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
            res = self.run(["-u", *args])
        except TuiError:
            return None
        if res.returncode:
            return [] if absent(res) else None
        return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]

    def _last_lines(self, target: str) -> str | None:
        try:
            return self.read(target, LAST_LINES) or None
        except TuiError:
            return None
