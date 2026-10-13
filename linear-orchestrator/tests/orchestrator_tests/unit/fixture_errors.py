"""Unexpected commands in strict unit test fakes."""


class CommandAssertion(AssertionError):
    @classmethod
    def unexpected(cls, argv):
        return cls(f"unexpected {argv}")

    @classmethod
    def git_ran(cls, argv):
        return cls(f"git ran: {argv}")
