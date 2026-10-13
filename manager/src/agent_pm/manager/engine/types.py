"""The manager's types: crew.py's Spec, crew.py's and roster.py's errors."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    """One worker for crew.start: `resume` the session id it resumes; `client` None: the caller's runtime, else the
    config's; `overrides` KEY=VALUE env entries; `note` its roster note."""

    name: str
    cwd: str
    prompt: str | None = None
    note: str | None = None
    flags: tuple[str, ...] = ()
    client: str | None = None
    overrides: tuple[str, ...] = ()
    resume: str | None = None


class RosterError(Exception):
    pass


class Stale(RosterError):
    pass
