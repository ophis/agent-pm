#!/usr/bin/env python3
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive import STATUSES, append_line, stamp  # noqa: E402


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="report.py")
    ap.add_argument("--to", required=True, help="the channel file")
    sub = ap.add_subparsers(dest="kind", required=True)
    p = sub.add_parser("progress")
    p.add_argument("name")
    p.add_argument("text", nargs=argparse.REMAINDER)
    o = sub.add_parser("outcome")
    o.add_argument("--status", required=True, choices=STATUSES)
    o.add_argument("--title", required=True)
    o.add_argument("--summary", required=True)
    o.add_argument("--question", action="append", dest="questions")
    o.add_argument("--url")
    o.add_argument("--file", action="append", dest="files")
    o.add_argument("--deliverable")
    s = sub.add_parser("stop")
    s.add_argument("--pending", metavar="KEY")
    return ap.parse_args(argv)


def pending(key: str) -> int | None:
    try:
        data = json.load(sys.stdin)
    except (OSError, ValueError):
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return len(value) if isinstance(value, list) else None


def main(argv: list[str]) -> int:
    a = parse(argv)
    try:
        if a.kind == "progress":
            line = {"kind": "progress", "name": a.name, "text": " ".join(a.text)}
        elif a.kind == "stop":
            line = {"kind": "stop"}
            if a.pending is not None and (n := pending(a.pending)) is not None:
                line["pending"] = n
        else:
            data = {
                k: v
                for k in ("status", "title", "summary", "questions", "url", "files")
                if (v := getattr(a, k)) is not None
            }
            if a.deliverable is not None:
                with open(a.deliverable) as f:
                    data["deliverable"] = f.read()
            line = {"kind": "outcome", "outcome": data}
        append_line(a.to, json.dumps({"ts": stamp(), **line}, ensure_ascii=False) + "\n")
    except (OSError, UnicodeDecodeError) as e:
        print(f"report.py: {e}", file=sys.stderr)
        return 1
    print(f"report.py: {a.kind} reported")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
