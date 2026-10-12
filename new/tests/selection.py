from agent_pm.session.driver.selection import configure


def configured(cls, config):
    return configure(cls(config))
