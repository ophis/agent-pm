"""Legacy forwarding path for Delegate configuration; use agent_pm.delegate.config."""

from agent_pm.delegate.config import CONFIG, harness_host, policy, read_config, trusted_dirs

__all__ = ["CONFIG", "harness_host", "policy", "read_config", "trusted_dirs"]
