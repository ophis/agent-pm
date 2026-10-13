import threading


class Late:
    """Holds the first call until released, then sends it: a write that timed out and lands later."""

    def __init__(self, gql):
        self.gql, self.first = gql, True
        self.held, self.release, self.landed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

    def __call__(self, query, **v):
        if not self.first:
            return self.gql(query, **v)
        self.first = False
        self.held.set()
        self.release.wait(30)
        try:
            return self.gql(query, **v)
        finally:
            self.landed.set()
