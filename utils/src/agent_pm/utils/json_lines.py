"""Parse object records from tolerant JSON line streams."""

import json
from collections.abc import Iterable, Iterator


def json_records(lines: Iterable[str | bytes]) -> Iterator[dict]:
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            yield record
