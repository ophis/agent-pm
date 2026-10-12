from agent_pm.session.adapters.selection import configure


def configured(cls, config):
    return configure(cls(config))
