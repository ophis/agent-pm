from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class Moved(FakeLinear):
    def __init__(self, issues, to):
        super().__init__(issues)
        self.to = to

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for i in self.issues.values():
                i["state"] = self.to
        return out
