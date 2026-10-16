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

PAGE = f"""<!DOCTYPE html>
<html><head>
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

    def test_every_failure_is_listed(self):
        markup = '<img src="https://x.com/a"><img src="https://y.com/b" onerror="f(\'z\')">'
        self.assertEqual(
            self.check(markup),
            (
                1,
                "external load: https://x.com/a\nexternal load: https://y.com/b\n"
                "unsafe markup: onerror=f('z')\n",
            ),
        )

    def test_an_unreadable_file_exits_two(self):
        out = io.StringIO()
        self.assertEqual(html_check.main(["/nonexistent/r.html"], out=out), 2)
        self.assertTrue(out.getvalue().startswith("check failed: "), out.getvalue())
