"""Argument helpers: Once rejects repeated options; process option validation is side-effect free."""

import argparse
import re
from collections.abc import Mapping


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given twice")
        setattr(namespace, self.dest, values)


def clean_string(value) -> bool:
    return isinstance(value, str) and "\0" not in value


def environment_ok(env) -> bool:
    return isinstance(env, Mapping) and all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and clean_string(v)
        for k, v in env.items()
    )


def process_options_error(*, flags, env) -> str | None:
    if not (isinstance(flags, tuple) and all(map(clean_string, flags))):
        return "flags must be a tuple of strings without NUL"
    if not environment_ok(env):
        return "env must map variable names to strings without NUL"
    return None
