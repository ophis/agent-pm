"""The Handoff's reader-side helpers: brief and is_user."""


def is_user(note) -> bool:
    return note.origin == "other"


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description
