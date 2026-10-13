"""Outcome data and validation failures shared by the delegate's callers."""

from dataclasses import dataclass, field
from typing import Literal, get_args

OutcomeStatus = Literal["done", "needs_input", "failed"]
STATUSES = get_args(OutcomeStatus)


class InvalidOutcome(Exception):
    @classmethod
    def not_json(cls, error):
        return cls(f"not JSON: {error}")

    @classmethod
    def not_object(cls, where=None):
        return cls("not an object" if where is None else f"{where} must be an object")

    @classmethod
    def both_deliverables(cls):
        return cls("give deliverable or deliverable_file, not both")

    @classmethod
    def status(cls, value):
        return cls(f"status {value!r} is not one of {', '.join(STATUSES)}")

    @classmethod
    def title(cls):
        return cls("title must be one line of printable text")

    @classmethod
    def text_fields(cls):
        return cls("summary and deliverable must be text")

    @classmethod
    def lists(cls):
        return cls("questions and files must be lists")

    @classmethod
    def questions(cls):
        return cls("needs_input must carry 1–4 questions")

    @classmethod
    def missing_deliverable(cls, destination):
        return cls(f"done with a {destination} destination must carry the deliverable")

    @classmethod
    def missing_field(cls, where, field):
        return cls(f"{where} lacks {field!r}")

    @classmethod
    def unknown_field(cls, field):
        return cls(f"unknown field {field[:100]!r}")

    @classmethod
    def text(cls, where):
        return cls(f"{where} must be text")

    @classmethod
    def too_short(cls, where, limit):
        return cls(f"{where} is shorter than {limit} characters")

    @classmethod
    def too_long(cls, where, limit):
        return cls(f"{where} is longer than {limit} characters")

    @classmethod
    def pattern(cls, where, pattern):
        return cls(f"{where} must match {pattern}")

    @classmethod
    def list(cls, where):
        return cls(f"{where} must be a list")

    @classmethod
    def too_many_items(cls, where, limit):
        return cls(f"{where} has more than {limit} items")

    @classmethod
    def enum(cls, where, value, options):
        return cls(f"{where} {value!r} is not one of {', '.join(map(str, options))}")

    @classmethod
    def plain_url(cls):
        return cls("url must be a plain https link")

    @classmethod
    def url_prefix(cls, host, prefix):
        return cls(f"url must start with https://{host}{prefix}")

    @classmethod
    def pull_request_url(cls):
        return cls("url must be a pull request, compare or tree link")

    @classmethod
    def file(cls, path):
        return cls(f"file {str(path)[:200]!r} is not a single-link .md file under the workdir")

    @classmethod
    def not_regular(cls, path):
        return cls(f"deliverable_file {str(path)[:200]!r}: not a single-link regular file")

    @classmethod
    def relocated(cls, path):
        return cls(f"deliverable_file {str(path)[:200]!r}: no longer under the workdir")

    @classmethod
    def replaced(cls, path):
        return cls(f"deliverable_file {str(path)[:200]!r}: replaced while it was opened")

    @classmethod
    def unreadable(cls, path, error):
        reason = error.strerror or "unreadable"
        return cls(f"deliverable_file {str(path)[:200]!r}: {reason}")

    @classmethod
    def large_file(cls, path, cap):
        return cls(f"deliverable_file {str(path)[:200]!r}: too large (over {cap} bytes)")

    @classmethod
    def file_encoding(cls, path):
        return cls(f"deliverable_file {str(path)[:200]!r}: not UTF-8 text")


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    url: str = ""
    files: list[str] = field(default_factory=list)
    deliverable: str = ""
