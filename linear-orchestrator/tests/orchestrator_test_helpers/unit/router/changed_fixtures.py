from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class Changed(FakeLinear):
    """Each list read is followed by `after`'s edits ({ident: fields}): a change before the claim's
    recheck.
    """

    def __init__(self, issues, after):
        super().__init__(issues)
        self.after = after

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for ident, fields in self.after.items():
                self.issues[ident].update(fields)
        return out
