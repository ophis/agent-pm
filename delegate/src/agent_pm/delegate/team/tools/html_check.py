#!/usr/bin/env python3
"""Checks a generated HTML report before it is committed: it may load nothing external but Google
Fonts and hold no script URL or report text in an event handler.

    html_check.py HTML

Prints `ok` and exits 0, else one line per failure and exits 1:
- `external load: <url>`: a URL whose host (urllib's, so userinfo and port are dropped) is neither
  fonts.googleapis.com nor fonts.gstatic.com. Matched case-insensitively as `http:` or `https:`
  followed by any mix of `/`, `\\` and whitespace, or none, anywhere in an attribute value (entities
  decoded) or a <script> or <style> body; or as a scheme-relative `//host` (either slash may be `\\`)
  at a value's start or after whitespace, a comma or `(`, or in a body after a quote or `(`. Not
  checked: `href` and `xlink:href` on <a> (links, not loads), `xmlns` and `xmlns:*` (namespace names),
  visible text and comments.
- `unsafe markup: <attr>=<value>`: a value holding `javascript:` once whitespace and control
  characters are dropped, or an `on*` value holding a quote or backtick (report text can enter a
  handler only as a string literal).
Exits 2, printing `check failed: <error>`, when HTML can't be read.

shortcut: CSS escapes (`\\68ttps:`) and JS-built URLs are not decoded; the generator is told to load
nothing external, so this is a backstop.
"""

import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit

FONTS = {"fonts.googleapis.com", "fonts.gstatic.com"}
_HOST = r"(?P<host>[^\s/\\\"'`()<>?#]+)[^\s\"'`()<>]*"
_SCHEME = r"https?:[\s/\\]*"
ATTR_URL = re.compile(rf"(?i)(?P<url>(?:{_SCHEME}|(?:^|(?<=[\s,(]))[/\\]{{2}}){_HOST})")
BODY_URL = re.compile(rf"(?i)(?P<url>(?:{_SCHEME}|(?<=[\"'`(])\s*[/\\]{{2}}){_HOST})")
LINKS = {"href", "xlink:href"}


class _Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.failures: list[str] = []
        self.body = False

    def _urls(self, pattern, text):
        for m in pattern.finditer(text):
            if urlsplit("//" + m["host"]).hostname not in FONTS:
                self.failures.append(f"external load: {m['url'].strip()}")

    def handle_starttag(self, tag, attrs):
        self.body = tag in ("script", "style")
        for name, value in attrs:
            if value is None:
                continue
            bare = re.sub(r"[\s\x00-\x1f\x7f]", "", value).lower()
            if not (tag == "a" and name in LINKS) and not re.fullmatch(r"xmlns(:.*)?", name):
                self._urls(ATTR_URL, value)
            if "javascript:" in bare or (name.startswith("on") and re.search("[\"'`]", value)):
                self.failures.append(f"unsafe markup: {name}={value}")

    def handle_endtag(self, tag):
        self.body = False

    def handle_data(self, data):
        if self.body:
            self._urls(BODY_URL, data)


def main(argv: list[str], *, out=sys.stdout) -> int:
    if len(argv) != 1:
        print("check failed: usage: html_check.py HTML", file=out)
        return 2
    try:
        with open(argv[0], encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"check failed: {e}", file=out)
        return 2
    check = _Check()
    check.feed(text)
    check.close()
    print("\n".join(check.failures) or "ok", file=out)
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
