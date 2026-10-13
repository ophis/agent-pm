"""Claude dialogue arguments inserted around session selection and access grants."""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class ClaudeCommand:
    head: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    tail: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
