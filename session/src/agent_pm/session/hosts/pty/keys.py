"""send --key's notation (KeyPress) as the bytes an xterm sends in its normal cursor mode."""

from agent_pm.session.engine.types import ConfigError, KeyPress, NamedKey

PLAIN: dict[str, bytes] = {
    NamedKey.ENTER: b"\r",
    NamedKey.ESCAPE: b"\x1b",
    NamedKey.TAB: b"\t",
    NamedKey.BTAB: b"\x1b[Z",
    NamedKey.BSPACE: b"\x7f",
}
# CSI <letter>, modified CSI 1;<m> <letter>; F1-F4 unmodified are SS3 <letter>
LETTERED: dict[str, str] = {
    NamedKey.UP: "A",
    NamedKey.DOWN: "B",
    NamedKey.RIGHT: "C",
    NamedKey.LEFT: "D",
    NamedKey.HOME: "H",
    NamedKey.END: "F",
    NamedKey.F1: "P",
    NamedKey.F2: "Q",
    NamedKey.F3: "R",
    NamedKey.F4: "S",
}
NUMBERED: dict[str, int] = {  # CSI <n> ~, modified CSI <n>;<m> ~
    NamedKey.PAGE_UP: 5,
    NamedKey.PAGE_DOWN: 6,
    NamedKey.F5: 15,
    NamedKey.F6: 17,
    NamedKey.F7: 18,
    NamedKey.F8: 19,
    NamedKey.F9: 20,
    NamedKey.F10: 21,
    NamedKey.F11: 23,
    NamedKey.F12: 24,
}
CTRL_PUNCTUATION = {"@": 0x00, " ": 0x00, "[": 0x1B, "\\": 0x1C, "]": 0x1D, "^": 0x1E}
CTRL_PUNCTUATION |= {"_": 0x1F, "/": 0x1F, "?": 0x7F}
SS3 = (NamedKey.F1, NamedKey.F2, NamedKey.F3, NamedKey.F4)


def encode(key: KeyPress) -> bytes:
    """ConfigError for a key xterm cannot send with its modifiers."""
    name = key.key
    modifier = 1 + key.shift + 2 * key.meta + 4 * key.ctrl
    if name in LETTERED:
        if modifier > 1:
            return f"\x1b[1;{modifier}{LETTERED[name]}".encode()
        return f"\x1b{'O' if name in SS3 else '['}{LETTERED[name]}".encode()
    if name in NUMBERED:
        return f"\x1b[{NUMBERED[name]}{f';{modifier}' if modifier > 1 else ''}~".encode()
    escape = b"\x1b" if key.meta else b""
    if name in PLAIN:
        if key.ctrl or key.shift and name != NamedKey.TAB:
            raise _refused(key)
        return escape + PLAIN[NamedKey.BTAB if key.shift else name]
    if key.shift:
        raise _refused(key)
    if not key.ctrl:
        return escape + name.encode()
    if name.isascii() and name.isalpha():
        return escape + bytes([ord(name.lower()) & 0x1F])
    if name in CTRL_PUNCTUATION:
        return escape + bytes([CTRL_PUNCTUATION[name]])
    raise _refused(key)


def _refused(key: KeyPress) -> ConfigError:
    return ConfigError(f"key {key}: the pty host cannot send it")
