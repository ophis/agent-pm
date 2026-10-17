"""svg_check.py verdicts on report figures."""

import importlib.util
import io
import os
import tempfile

from agent_pm.delegate.composer.base import TEAM

from delegate.tests.case import Case

SPEC = importlib.util.spec_from_file_location(
    "svg_check", os.path.join(TEAM, "tools", "svg_check.py")
)
assert SPEC and SPEC.loader
svg_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(svg_check)

OPEN = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'

FIGURE = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- generated -->
{OPEN}
<defs><linearGradient id="g"><stop offset="0"/></linearGradient>
<marker id="m"><path d="M0 0"/></marker></defs>
<style>.a {{ fill: url(#g); }} .b {{ fill: url( '#g' ); }}</style>
<rect width="10" height="10" fill="url(#g)" style="stroke: url(&quot;#g&quot;)"/>
<line marker-end="url(#m)"/>
<use href="#m"/><use xlink:href="#m"/>
<text>See https://example.com and url(http://example.com) &amp; onload</text>
</svg>
"""


class SvgCheck(Case):
    def check(self, svg):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r-1.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
            out = io.StringIO()
            return svg_check.main([path], out=out), out.getvalue()

    def test_a_self_contained_figure_passes(self):
        self.assertEqual(self.check(FIGURE), (0, "ok\n"))

    def test_rejections(self):
        cases = (
            ("<script>alert(1)</script>", "unsafe markup: <script>"),
            ('<svg:script xmlns:svg="http://www.w3.org/2000/svg"/>', "unsafe markup: <script>"),
            ("<foreignObject><div/></foreignObject>", "unsafe markup: <foreignObject>"),
            ('<rect onclick="go()"/>', "unsafe markup: onclick"),
            ('<rect ONLOAD="go()"/>', "unsafe markup: ONLOAD"),
            ('<image href="https://x.com/i.png"/>', "external reference: https://x.com/i.png"),
            ('<a xlink:href="r.md"><text>r</text></a>', "external reference: r.md"),
            (
                '<image href="data:image/png;base64,AA"/>',
                "external reference: data:image/png;base64,AA",
            ),
            (
                '<use href="&#104;ttps://x.com/s.svg#a"/>',
                "external reference: https://x.com/s.svg#a",
            ),
            ('<rect fill="url(https://x.com/p)"/>', "external reference: url(https://x.com/p)"),
            ("<rect style=\"fill:url( '//x.com/p' )\"/>", "external reference: url( '//x.com/p' )"),
            ("<style>@import 'https://x.com/s.css';</style>", "external reference: @import"),
            (
                r"<style>.a { fill: u\72l(https://x.com/p) }</style>",
                "external reference: url(https://x.com/p)",
            ),
        )
        for markup, line in cases:
            with self.subTest(markup):
                self.assertEqual(self.check(f"{OPEN}{markup}</svg>"), (1, line + "\n"))

    def test_preamble_rejections(self):
        cases = (
            (
                '<!DOCTYPE svg [<!ENTITY e "https://x.com">]>' + OPEN + "</svg>",
                "unsafe markup: <!DOCTYPE>",
            ),
            (
                '<?xml-stylesheet href="https://x.com/s.css"?>' + OPEN + "</svg>",
                "unsafe markup: <?xml-stylesheet?>",
            ),
        )
        for svg, line in cases:
            with self.subTest(svg):
                self.assertEqual(self.check(svg)[0], 1)
                self.assertEqual(self.check(svg)[1].splitlines()[0], line)

    def test_every_failure_is_listed(self):
        self.assertEqual(
            self.check(f'{OPEN}<script/><rect onclick="x" href="y"/></svg>'),
            (1, "unsafe markup: <script>\nunsafe markup: onclick\nexternal reference: y\n"),
        )

    def test_not_an_svg(self):
        for text, start in (
            ("<svg", "invalid svg: "),
            ("<html/>", "invalid svg: root <html>"),
            ("<svg/>", "invalid svg: root <svg>"),
        ):
            with self.subTest(text):
                code, out = self.check(text)
                self.assertEqual(code, 1)
                self.assertTrue(out.startswith(start), out)

    def test_the_size_cap(self):
        padding = "<!--" + "x" * svg_check.MAX_BYTES + "-->"
        self.assertEqual(
            self.check(f"{OPEN}{padding}</svg>"),
            (1, f"too large: {len(OPEN) + len(padding) + 6} bytes\n"),
        )

    def test_an_unreadable_file_or_bad_usage_exits_two(self):
        out = io.StringIO()
        self.assertEqual(svg_check.main(["/nonexistent/r-1.svg"], out=out), 2)
        self.assertTrue(out.getvalue().startswith("check failed: "), out.getvalue())
        out = io.StringIO()
        self.assertEqual(svg_check.main([], out=out), 2)
        self.assertEqual(out.getvalue(), "check failed: usage: svg_check.py SVG\n")
