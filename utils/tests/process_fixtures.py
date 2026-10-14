"""Standard-library exception fixtures without importing launch APIs into unit tests."""

from subprocess import TimeoutExpired

__all__ = ["TimeoutExpired"]
