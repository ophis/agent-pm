"""Usage thresholds of the board's gate and an account pick."""

MAX_5H = 90
MAX_WEEK = 100


def exhausted(usage, max_5h=MAX_5H):
    five, week = usage.five_hour, usage.weekly
    return (five is not None and five.used_percent >= max_5h) or (
        week is not None and week.used_percent >= MAX_WEEK
    )
