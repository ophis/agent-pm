"""Types used by both crew.py and roster.py."""


class RosterError(Exception):
    pass


class Stale(RosterError):
    pass
