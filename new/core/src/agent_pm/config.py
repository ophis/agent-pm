"""The core config's paths, their only home: CONFIG, the core's config.toml; LOCAL, this machine's settings on top."""

import os

from agent_pm.utils.paths import CORE

CONFIG = os.path.join(CORE, "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"
