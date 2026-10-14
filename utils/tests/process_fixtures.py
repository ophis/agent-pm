"""Standard-library exception fixtures without importing launch APIs into unit tests."""

from subprocess import DEVNULL, TimeoutExpired

__all__ = ["DEVNULL", "TimeoutExpired"]
