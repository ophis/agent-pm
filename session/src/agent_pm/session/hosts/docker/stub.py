"""The container's hook command (stdlib only, run by path as `<hook path> hook <dir> <label>`): sends
its argv and stdin to the relay at SOCKET and waits WAIT seconds for the ack. It always exits 0, with
one stderr line when the relay is absent or silent, so a missing relay never fails the client.
"""

import base64
import json
import os
import socket
import sys

SOCKET = "/tmp/agent-pm-relay.sock"
WAIT = 10


def main(argv=None, stdin=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    payload = (sys.stdin.buffer if stdin is None else stdin).read()
    request = {
        "id": int.from_bytes(os.urandom(6), "big"),
        "argv": args,
        "payload": base64.b64encode(payload).decode(),
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(WAIT)
            conn.connect(SOCKET)
            conn.sendall(json.dumps(request).encode() + b"\n")
            with conn.makefile("rb") as reader:
                acked = reader.readline(4096).endswith(b"\n")
    except OSError as error:
        print(f"agent-pm hook relay: {error}", file=sys.stderr)
        return 0
    if not acked:
        print("agent-pm hook relay: no ack", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
