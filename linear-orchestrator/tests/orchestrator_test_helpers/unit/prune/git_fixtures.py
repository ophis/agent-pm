from agent_pm.utils import sh


class Git:
    def __init__(self, real=False):
        self.real, self.calls = real, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if not self.real:
            raise AssertionError(f"git ran: {argv}")
        return sh(argv, timeout)
