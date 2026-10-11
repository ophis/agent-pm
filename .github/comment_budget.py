import ast
from collections import namedtuple
import io
import math
from pathlib import Path
import re
import subprocess
import sys
import tokenize

ROOT = Path(__file__).resolve().parents[1]
Rule = namedtuple("Rule", "name pattern limit")
SOURCE = Rule("source", re.compile(""), 15)
TESTS = Rule("tests", re.compile(r"(^|/)tests/|_test\.py$"), 10)
PURPOSE = ("The comment budget keeps comments short: meet it by cutting comments or docstrings, "
           "never by adding code to lower the ratio.")


def chars(text):
    return len("".join(text.split()))


def bare_string(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def measure(text):
    tree = ast.parse(text)
    docstring = sum(chars(ast.get_source_segment(text, node)) for node in ast.walk(tree) if bare_string(node))
    comment = sum(chars(token.string) for token in tokenize.generate_tokens(io.StringIO(text).readline)
                  if token.type == tokenize.COMMENT and not (token.start[0] == 1 and token.string.startswith("#!")))
    return comment, docstring, chars(text) - comment - docstring


def rule(path):
    return next(each for each in (TESTS, SOURCE) if each.pattern.search(path))


def tracked(root):
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"], capture_output=True, text=True)
    if listed.returncode:
        raise ValueError(f"git ls-files failed: {listed.stderr.strip()}")
    return sorted(path for path in listed.stdout.split("\0") if path and (root / path).is_file())


def percent(n, code):
    return f"{n / code:.2%}" if code else "inf" if n else "0.00%"


def main(root=ROOT):
    root = Path(root)
    files = {SOURCE: [], TESTS: []}
    try:
        for path in tracked(root):
            try:
                with tokenize.open(root / path) as file:
                    comment, docstring, code = measure(file.read())
            except (OSError, SyntaxError, ValueError, tokenize.TokenError) as error:
                raise ValueError(f"{path}: {error}") from error
            files[rule(path)].append((path, comment + docstring, code))
    except ValueError as error:
        print(f"comment_budget: {error}", file=sys.stderr)
        return 2
    failed = False
    for each in (SOURCE, TESTS):
        n, code = sum(entry[1] for entry in files[each]), sum(entry[2] for entry in files[each])
        totals = f"({n} comment+docstring / {code} code characters"
        if n * 100 <= each.limit * code:
            print(f"ok {each.name}: {percent(n, code)} <= {each.limit}% {totals})")
            continue
        failed = True
        over = n - each.limit * code // 100
        print(f"FAIL {each.name}: {percent(n, code)} > {each.limit}% {totals}; {over} over budget)")
        ranked = sorted((entry for entry in files[each] if entry[1]),
                        key=lambda entry: (-entry[1] / entry[2] if entry[2] else -math.inf, entry[0]))
        for path, kept, size in ranked[:10]:
            print(f"  {percent(kept, size)} {path}")
    if failed:
        print(PURPOSE)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
