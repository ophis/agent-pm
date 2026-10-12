from agent_pm.session.selection import configure


def configured(cls, config):
    return configure(cls(config))
