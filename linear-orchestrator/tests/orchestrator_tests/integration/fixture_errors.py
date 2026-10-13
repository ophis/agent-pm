"""Failures while setting up fake services and running a scenario."""

from agent_pm.linear_orchestrator.engine.types import LinearError


class StepFailure(AssertionError):
    def __init__(self, number, name, cause):
        super().__init__(f"failed at step {number} ({name}): {cause}")


class Problem(Exception):
    @classmethod
    def unknown_field(cls, op, field):
        return cls(f"fake: {op} has no field {field}")

    @classmethod
    def missing_entity(cls, entity):
        return cls(f"Entity not found: {entity}")

    @classmethod
    def missing_entities(cls, entities):
        return cls(f"Entity not found: {', '.join(entities[:-1])} or {entities[-1]}")

    @classmethod
    def invalid_issue(cls, id, identifier, prefix):
        return cls(f"fake: issue id {id} or identifier {identifier} in use, or not {prefix}-<n>")

    @classmethod
    def duplicate_comment(cls, cid):
        return cls(f"fake: comment id {cid} in use")

    @classmethod
    def unknown_filter(cls, field):
        return cls(f"fake: no issue filter {field}")

    @classmethod
    def unknown_comparator(cls, comparator):
        return cls(f"fake: no comparator {comparator}")

    @classmethod
    def foreign_comment(cls):
        return cls("fake: commentUpdate: only its author edits a comment")

    @classmethod
    def unknown_create_input(cls, field):
        return cls(f"fake: issueCreate has no input {field}")


class FixtureError(ValueError):
    @classmethod
    def conflicting_operations(cls, first, second):
        return cls(f"one query text, two ops: {first}, {second}")

    @classmethod
    def duplicate_user(cls, email, service):
        return cls(f"user {email} or service {service} seeded twice")

    @classmethod
    def unseeded_access(cls):
        return cls("assignee or a label not seeded")

    @classmethod
    def unseeded_user(cls, user):
        return cls(f"user {user} not seeded")

    @classmethod
    def unseeded_issue(cls, issue):
        return cls(f"issue {issue} not seeded")

    @classmethod
    def invalid_failure(cls, op, kind, account, times):
        return cls(f"fail({op!r}, {kind!r}, account={account!r}, times={times!r}): a misuse")


class FakeCredentialError(LinearError):
    def __init__(self, service):
        super().__init__(f"fake transport: no key for {service}")
