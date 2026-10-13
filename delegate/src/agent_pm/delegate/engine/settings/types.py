"""Malformed Delegate settings retain ValueError catches and distinguish incorrect value types."""


class SettingsError(ValueError):
    pass


class SettingsTypeError(TypeError, SettingsError):
    pass
