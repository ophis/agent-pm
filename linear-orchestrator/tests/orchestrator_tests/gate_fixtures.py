"""Process-free Delegate quota providers shared by Gate boundary tests."""

from dataclasses import replace

from agent_pm.session import Usage, Window

from delegate.tests.fakes.delegate import FakeDelegate


def quota_provider(runtime, percent):
    class QuotaDelegate(FakeDelegate):
        ROLES = {"engineer": {"build": "Build"}, "pm": {"design": "Design"}}
        queried = []

        @classmethod
        def resolve(cls, role, task=None, *, overlays=()):
            return replace(super().resolve(role, task, overlays=overlays), runtime=runtime)

        @classmethod
        def usage(cls, role):
            cls.queried.append(role)
            return Usage(Window(percent, None), None)

    return QuotaDelegate
