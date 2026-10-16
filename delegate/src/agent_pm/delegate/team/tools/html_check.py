#!/usr/bin/env python3
"""Checks a generated HTML report: it may load nothing but Google Fonts.

    html_check.py HTML

`ok`, exit 0; else exit 1, one line per failure: `external load: <url>` (an http(s) or `//` URL
outside <a href>, xmlns, namespace names and text) or `unsafe markup: <attr>=<value>`
(`javascript:`, or a quote in an `on*` value). Unreadable: `check failed: <error>`, exit 2.
shortcut: CSS escapes and JS-built URLs are not decoded; the prompt forbids external loads.
"""

import html
import re
import sys
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

FONTS = {"fonts.googleapis.com", "fonts.gstatic.com"}
_HOST = r"(?P<host>[^\s/\\\"'`()<>?#]+)[^\s\"'`()<>]*"
_SCHEME = r"https?:[\s/\\]*"
ATTR_URL = re.compile(rf"(?i)(?P<url>(?:{_SCHEME}|(?:^|(?<=[\s,(]))[/\\]{{2}}){_HOST})")
BODY_URL = re.compile(rf"(?i)(?P<url>(?:{_SCHEME}|(?<=[\"'`(])\s*[/\\]{{2}}){_HOST})")
LINKS = {"href", "xlink:href"}
NAMESPACES = {f"http://www.w3.org/{n}" for n in ("2000/svg", "1999/xlink", "1999/xhtml")}


class _Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.failures: list[str] = []
        self.body = False
        self.foreign = 0

    def _urls(self, pattern, text):
        for m in pattern.finditer(text):
            if m["url"] in NAMESPACES:
                continue
            if urlsplit("//" + m["host"]).hostname not in FONTS:
                self.failures.append(f"external load: {m['url'].strip()}")

    def handle_starttag(self, tag, attrs):
        self.body = tag in ("script", "style")
        self.foreign += tag in ("svg", "math")
        for name, value in attrs:
            if value is None:
                continue
            bare = re.sub(r"[\s\x00-\x1f\x7f]", "", value).lower()
            if not (tag == "a" and name in LINKS) and not re.fullmatch(r"xmlns(:.*)?", name):
                self._urls(ATTR_URL, unquote(value))
            if "javascript:" in bare or (name.startswith("on") and re.search("[\"'`]", value)):
                self.failures.append(f"unsafe markup: {name}={value}")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.body and not self.foreign:
            self.set_cdata_mode(tag)
        else:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.body = False
        if tag in ("svg", "math") and self.foreign:
            self.foreign -= 1

    def handle_data(self, data):
        if self.body:
            self._urls(BODY_URL, html.unescape(data) if self.foreign else data)


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
