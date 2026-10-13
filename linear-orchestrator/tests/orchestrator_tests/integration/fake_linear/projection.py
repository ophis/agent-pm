import functools
import re

from orchestrator_tests.integration.fixture_errors import Problem


@functools.cache
def selection(query):
    tokens = re.findall(r"\w+|[{}()]", query)

    def skip(i):
        depth = 0
        while True:
            depth += {"(": 1, ")": -1}.get(tokens[i], 0)
            i += 1
            if depth == 0:
                return i

    def block(i):
        fields, i, last = {}, i + 1, None
        while tokens[i] != "}":
            if tokens[i] == "(":
                i = skip(i)
            elif tokens[i] == "{":
                fields[last], i = block(i)
            else:
                last, i = tokens[i], i + 1
                fields[last] = None
        return fields, i + 1

    i = 0
    while tokens[i] != "{":
        i = skip(i) if tokens[i] == "(" else i + 1
    return block(i)[0]


def project(value, sel, op):
    if sel is None or value is None:
        return value
    if isinstance(value, list):
        return [project(v, sel, op) for v in value]
    if missing := [f for f in sel if f not in value]:
        raise Problem.unknown_field(op, missing[0])
    return {f: project(value[f], s, op) for f, s in sel.items()}
