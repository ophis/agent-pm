"""The Driver's own error, DriverError: here so the Modes (modes/) raise it without importing driver.api, which
re-exports it (api → modes stays one-way)."""


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""
