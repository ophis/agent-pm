# Core pack lives in the agent-pm repo

The core pack is a Claude Code plugin in the `core/` directory of this repo, published through a repo-root marketplace, rather than a separate repo. While the core/orchestration interface is still moving, one PR can change both sides with no version pinning between repos; `git subtree split` extracts it when it is open-sourced.
