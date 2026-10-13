"""Register or restore one synthetic module without removing unrelated imports."""

import sys
from contextlib import contextmanager


@contextmanager
def module_slot(name, module=None):
    previous = sys.modules.get(name)
    present = name in sys.modules
    if module is not None:
        sys.modules[name] = module
    try:
        yield
    finally:
        if present:
            sys.modules[name] = previous
        else:
            sys.modules.pop(name, None)
