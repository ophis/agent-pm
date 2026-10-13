"""load(path, base): the class a `module.path:Class` names (PATH), a concrete subclass of base; else
ValueError.
"""

import importlib
import inspect
import re

PATH = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z")


class ClassError(ValueError):
    @classmethod
    def reference(cls, path):
        return cls(f"{path}: want module.path:Class")

    @classmethod
    def importing(cls, path, module, error):
        return cls(f"{path}: cannot import {module}: {error}")

    @classmethod
    def absent(cls, path, module, name):
        return cls(f"{path}: {module} has no {name}")

    @classmethod
    def implementation(cls, path, base):
        return cls(f"{path} must be a concrete {base.__name__} subclass")


def load(path: str, base: type) -> type:
    if not isinstance(path, str) or not PATH.fullmatch(path):
        raise ClassError.reference(path)
    module, _, name = path.partition(":")
    try:
        found = importlib.import_module(module)
    except (ImportError, OSError, SyntaxError) as e:
        raise ClassError.importing(path, module, e) from e
    try:
        cls = getattr(found, name)
    except AttributeError:
        raise ClassError.absent(path, module, name) from None
    if (
        not isinstance(cls, type)
        or cls is base
        or not issubclass(cls, base)
        or inspect.isabstract(cls)
    ):
        raise ClassError.implementation(path, base)
    return cls
