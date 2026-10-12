"""DriverError, here so the Modes raise it without importing driver.api, which re-exports it."""


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""
