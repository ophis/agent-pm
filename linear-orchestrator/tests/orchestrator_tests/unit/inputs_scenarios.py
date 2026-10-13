
class Gh:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return self.table[argv[-1]]
