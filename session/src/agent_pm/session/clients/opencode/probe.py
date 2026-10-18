"""Prints Ollama's model listing (GET /api/tags) on 127.0.0.1:PORT as JSON, or null when it cannot
be read. Standalone: the standard library only.
"""

import http.client
import json
import sys

TIMEOUT = 5  # seconds
LIMIT = 1 << 20  # bytes of the listing


def listing(port: str) -> object:
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=TIMEOUT)
    try:
        connection.request("GET", "/api/tags")
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(LIMIT + 1)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    if len(body) > LIMIT:
        return None
    try:
        found = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


if __name__ == "__main__":
    print(json.dumps(listing(sys.argv[1]) if len(sys.argv) == 2 else None))
