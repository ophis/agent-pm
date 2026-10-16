#!/usr/bin/env python3
"""Checks a generated HTML report: it may load nothing but Google Fonts.

    html_check.py HTML

`ok`, exit 0; else exit 1, one line per failure: `external load: no CSP prefix` (the page must
start with PREFIX, so the browser enforces CSP), `external load: <url>` (an http(s) or `//` URL
outside <a href>, xmlns and text), or `unsafe markup: <attr>=<value>` (`javascript:`, a quote in
an `on*` value, refresh, dns-prefetch). Unreadable: `check failed: <error>`, exit 2.
shortcut: the URL scan models browser parsing roughly; PREFIX is what holds.
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
XMLNS = re.compile(r"(?i)xmlns(?::[\w.-]+)?\s*=\s*[\"']?$")
CSP = (
    "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src data:; script-src 'unsafe-inline'"
)
_WS = "[ \t\n\f\r]"
# A comment without <, > or - cannot end early (`<!-->`) to hide a tag.
_GAP = rf"(?:{_WS}|<!--[^<>-]*-->)*"
_ATTR = rf"""{_WS}+[a-z][\w:-]*(?:{_WS}*={_WS}*(?:"[^"<>]*"|'[^'<>]*'|[^ \t\n\f\r"'<>=`]+))?"""
PREFIX = re.compile(
    rf"""(?ai)\ufeff?{_GAP}<!doctype{_WS}+html{_WS}*>{_GAP}<html(?:{_ATTR})*{_WS}*>{_GAP}<head>"""
    rf"""{_GAP}<meta{_WS}+charset="utf-8"{_WS}*/?>{_GAP}<meta{_WS}+"""
    rf"""http-equiv="Content-Security-Policy"{_WS}+content="{re.escape(CSP)}"{_WS}*/?>"""
)
REFRESH = re.compile(r"(?i)http-equiv\s*=\s*[\"']?\s*refresh")


class _Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.failures: list[str] = []
        self.body = False
        self.foreign = 0

    def _urls(self, pattern, text):
        for m in pattern.finditer(text):
            if m["url"] in NAMESPACES and XMLNS.search(text, 0, m.start()):
                continue
            if urlsplit("//" + m["host"].rstrip(";,")).hostname not in FONTS:
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
    m = PREFIX.match(text)
    # Browsers seek <meta charset> in the first 1024 bytes only.
    failures = [] if m and len(m[0].encode()) <= 1024 else ["external load: no CSP prefix"]
    decoded = html.unescape(text)
    if REFRESH.search(decoded):
        failures.append("unsafe markup: http-equiv=refresh")
    if "dns-prefetch" in decoded.lower():
        failures.append("unsafe markup: rel=dns-prefetch")
    check = _Check()
    check.feed(text)
    check.close()
    failures += check.failures
    print("\n".join(failures) or "ok", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
