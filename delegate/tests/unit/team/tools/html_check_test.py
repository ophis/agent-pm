"""html_check.py verdicts on generated report pages."""

import importlib.util
import io
import os
import tempfile

from agent_pm.delegate.composer.base import TEAM

from delegate.tests.case import Case

SPEC = importlib.util.spec_from_file_location(
    "html_check", os.path.join(TEAM, "tools", "html_check.py")
)
assert SPEC and SPEC.loader
html_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(html_check)

FONTS = "https://fonts.googleapis.com/css2?family=Inter&display=swap"
CSP = (
    "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src data:; script-src 'unsafe-inline'"
)
PREFIX = (
    '<!doctype html><html><head><meta charset="utf-8">'
    f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
)

PAGE = f"""<!DOCTYPE html>
<html lang="en">
<!-- generated -->
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="{CSP}"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<style>@import url('{FONTS}'); body {{ font: 16px Inter; }}</style>
</head><body>
<h1>Report</h1>
<p>See https://example.com/a and //example.org/b in text.</p>
<!-- https://example.net/c -->
<a href="https://example.com/source">[1]</a>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<a xlink:href="https://example.com/x"><text>x</text></a></svg>
<button onclick="toggle(this)">Theme</button>
<script>// draw arrows
const s = 'https:'; document.body.dataset.x = s;</script>
</body></html>
"""


class HtmlCheck(Case):
    def check(self, html):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            out = io.StringIO()
            return html_check.main([path], out=out), out.getvalue()

    def test_a_plugin_shaped_page_passes(self):
        self.assertEqual(self.check(PAGE), (0, "ok\n"))

    def test_each_failure_is_one_line(self):
        cases = (
            (
                '<script src="https://cdn.x.com/a.js"></script>',
                "external load: https://cdn.x.com/a.js",
            ),
            ('<img srcset="a.png 1x, //x.com/b.png 2x">', "external load: //x.com/b.png"),
            ('<div style="background:url(//x.com/a)"></div>', "external load: //x.com/a"),
            (
                '<script type="importmap">{"imports": {"a": "https://x.com/a.js"}}</script>',
                "external load: https://x.com/a.js",
            ),
            ('<script>import "https://x.com/m.js";</script>', "external load: https://x.com/m.js"),
            (
                "<style>@import url(https://x.com/a.css);</style>",
                "external load: https://x.com/a.css",
            ),
            (
                "<style>p { background: url( //x.com/i.png) }</style>",
                "external load: //x.com/i.png",
            ),
            (
                '<link rel="stylesheet" href="https://fonts.googleapis.com@evil.com/">',
                "external load: https://fonts.googleapis.com@evil.com/",
            ),
            ('<script/>import("https://x.com/m.js")</script>', "external load: https://x.com/m.js"),
            (
                "<style/>@import url(https://x.com/a.css);</style>",
                "external load: https://x.com/a.css",
            ),
            (
                '<img src="http://www.w3.org/2000/svg.png">',
                "external load: http://www.w3.org/2000/svg.png",
            ),
            ('<img src="http://www.w3.org/2000/svg">', "external load: http://www.w3.org/2000/svg"),
            (
                '<meta http-equiv="&#114;efresh" content="0;url=/x">',
                "unsafe markup: http-equiv=refresh",
            ),
            ('<link rel="DNS-prefetch" href="/x">', "unsafe markup: rel=dns-prefetch"),
            ('<img src="HTTPS://x.com/p">', "external load: HTTPS://x.com/p"),
            ('<img src="https:x.com/p">', "external load: https:x.com/p"),
            ('<img src="https:\\\\x.com/p">', "external load: https:\\\\x.com/p"),
            ('<a href=" JavaScript:alert(1)">x</a>', "unsafe markup: href= JavaScript:alert(1)"),
            ("<button onclick=\"alert('x')\">x</button>", "unsafe markup: onclick=alert('x')"),
        )
        for markup, line in cases:
            with self.subTest(markup):
                self.assertEqual(
                    self.check(PAGE.replace("</body>", markup + "</body>")), (1, line + "\n")
                )

    def test_the_csp_prefix_is_required(self):
        missing = (
            ("no prefix", "<html><body>x</body></html>"),
            ("a load before the policy", PAGE.replace("<head>", f'<head><link href="{FONTS}">')),
            ("a comment hiding a tag", PREFIX.replace("<head>", "<!--><script src=x.js>--><head>")),
            ("a non-HTML space", PREFIX.replace("<head>", "\u00a0<head>")),
            ("a quote-swallowing attribute", PREFIX.replace("<html>", '<html x=">')),
            ("a widened policy", PREFIX.replace("default-src 'none'", "default-src https:")),
        )
        for name, page in missing:
            with self.subTest(name):
                code, out = self.check(page)
                self.assertEqual((code, out.splitlines()[0]), (1, "external load: no CSP prefix"))

    def test_prefix_variants_pass(self):
        for page in (
            PREFIX,
            "\ufeff \n" + PREFIX.replace("<head>", "\n<!-- a b -->\t<head>\r\n"),
            PREFIX.replace("<html>", "<html lang=en data-x='1'>").replace("utf-8", "UTF-8"),
        ):
            with self.subTest(page):
                self.assertEqual(self.check(page), (0, "ok\n"))

    def test_every_failure_is_listed(self):
        markup = '<img src="https://x.com/a"><img src="https://y.com/b" onerror="f(\'z\')">'
        self.assertEqual(
            self.check(PREFIX + markup),
            (
                1,
                "external load: https://x.com/a\nexternal load: https://y.com/b\n"
                "unsafe markup: onerror=f('z')\n",
            ),
        )

    def test_foreign_content_and_percent_encoding(self):
        cases = (
            (
                '<svg><script/></svg><img src="&#x68;ttps://x.com/a">'
                "<button onclick=\"go('x')\">b</button>",
                "external load: https://x.com/a\nunsafe markup: onclick=go('x')\n",
            ),
            (
                '<svg><script>import("&#x68;ttps://x.com/m.js")</script></svg>',
                "external load: https://x.com/m.js\n",
            ),
            (
                '<iframe src="data:text/html,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%3E'
                '%3Cimage%20href=%22https://x.com/i.png%22/%3E%3C/svg%3E"></iframe>',
                "external load: https://x.com/i.png\n",
            ),
            ('<img src="https%3A//x.com/p">', "external load: https://x.com/p\n"),
        )
        for markup, lines in cases:
            with self.subTest(markup):
                self.assertEqual(self.check(PREFIX + markup), (1, lines))

    def test_the_researcher_is_told_the_checked_prefix(self):
        with open(os.path.join(TEAM, "roles", "researcher.md"), encoding="utf-8") as f:
            self.assertIn(PREFIX.replace(CSP, html_check.CSP) + "`", f.read())

    def test_an_unreadable_file_exits_two(self):
        out = io.StringIO()
        self.assertEqual(html_check.main(["/nonexistent/r.html"], out=out), 2)
        self.assertTrue(out.getvalue().startswith("check failed: "), out.getvalue())
