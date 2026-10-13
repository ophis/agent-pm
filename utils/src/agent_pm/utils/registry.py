"""load(path, base): the class a `module.path:Class` names (PATH), a concrete subclass of base; else
ValueError.
"""

import importlib
import inspect
import re

PATH = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z")


class ClassError(ValueError):
    pass


def load(path: str, base: type) -> type:
    if not isinstance(path, str) or not PATH.fullmatch(path):
        raise ClassError(f"{path}: want module.path:Class")
    module, _, name = path.partition(":")
    try:
        found = importlib.import_module(module)
    except (ImportError, OSError, SyntaxError) as e:
        raise ClassError(f"{path}: cannot import {module}: {e}") from e
    try:
        cls = getattr(found, name)
    except AttributeError:
        raise ClassError(f"{path}: {module} has no {name}") from None
    if (
        not isinstance(cls, type)
        or cls is base
        or not issubclass(cls, base)
        or inspect.isabstract(cls)
    ):
        raise ClassError(f"{path} must be a concrete {base.__name__} subclass")
    return cls
