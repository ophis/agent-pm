import json

from agent_pm.linear_orchestrator.engine import config, inputs, target
from agent_pm.linear_orchestrator.linear import issues
from agent_pm.utils import SHORT
from orchestrator_tests.case import Case
from orchestrator_tests.unit.inputs_data import (
    BASE,
    DOCS,
    HUMANS,
    NOT_FOUND,
    RAW,
    endpoint,
    fenced,
    issue,
    lines,
    listing,
    ok,
)
from orchestrator_tests.unit.inputs_scenarios import Gh


class DocPath(Case):
    def test_accepted(self):
        for url, want in (
            (BASE + "Research/a.md", "Research/a.md"),
            (BASE + "Product%20Design/2026-09-01-PM-9-x.md", "Product Design/2026-09-01-PM-9-x.md"),
            (BASE + "a/b/c.md", "a/b/c.md"),
            (BASE + "%E6%8A%A5%E5%91%8A.md", "报告.md"),
        ):
            with self.subTest(url=url):
                self.assertEqual(inputs.doc_path(url, DOCS), want)

    def test_fragment_and_query_end_the_path(self):
        for suffix in ("#L10", "#L10-L20", "#section", "?plain=1", "?plain=1#L3", "#a/b?c"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    inputs.doc_path(BASE + "Product%20Design/x.md" + suffix, DOCS),
                    "Product Design/x.md",
                )
        self.assertEqual(inputs.doc_path(BASE + "x%23y%3Fz.md#L1", DOCS), "x#y?z.md")

    def test_rejected(self):
        for url in (
            "https://github.com/other/repo/blob/main/a.md",
            BASE + "#L10",
            BASE + "?plain=1",
            BASE + "../a.md#L1",
            BASE + "a.md#L1)",
            BASE + "a.md#L 1",
            "https://github.com/ophis/private_docs/blob/dev/a.md",
            "https://github.com/ophis/private_docs/tree/main/a.md",
            "https://example.com/a.md",
            BASE,
            BASE + "../a.md",
            BASE + "a/../b.md",
            BASE + "a/%2e%2e/b.md",
            BASE + "a%0Ab.md",
            BASE + "a.md)",
            BASE + "a b.md",
            "x " + BASE + "a.md",
        ):
            with self.subTest(url=url):
                self.assertIsNone(inputs.doc_path(url, DOCS))

    def test_branch_with_slash(self):
        docs = config.Docs("ophis/private_docs", "docs/main", {})
        self.assertEqual(
            inputs.doc_path("https://github.com/ophis/private_docs/blob/docs/main/a.md", docs),
            "a.md",
        )


class Gather(Case):
    def test_read_argv_and_text(self):
        gh = Gh(
            {
                endpoint("Research/2026-08-01-RES-2-a.md"): ok("# A\n"),
                endpoint("Research"): listing(),
            }
        )
        iss = issue(f"See {BASE}Research/2026-08-01-RES-2-a.md for background.")
        src = inputs.gather(iss, "researcher", DOCS, run=gh)
        self.assertEqual(
            src.docs,
            (
                inputs.Doc(
                    BASE + "Research/2026-08-01-RES-2-a.md",
                    "Research/2026-08-01-RES-2-a.md",
                    "# A\n",
                ),
            ),
        )
        self.assertIsNone(src.earlier)
        self.assertEqual(
            gh.calls,
            [
                (["gh", "api", endpoint("Research")], SHORT),
                (RAW + [endpoint("Research/2026-08-01-RES-2-a.md")], SHORT),
            ],
        )

    def test_path_is_quoted_not_the_query_separator(self):
        gh = Gh(
            {
                endpoint("Product%20Design/x%23y.md"): ok("t"),
                endpoint("Product%20Design"): listing(),
            }
        )
        iss = issue(f"{BASE}Product%20Design/x%23y.md")
        src = inputs.gather(iss, "pm", DOCS, run=gh)
        self.assertEqual([d.path for d in src.docs], [])
        self.assertEqual(src.earlier.path, "Product Design/x#y.md")
        self.assertEqual(gh.calls[-1][0][-1], endpoint("Product%20Design/x%23y.md"))

    def test_404_is_not_found_other_failure_raises(self):
        iss = issue(f"{BASE}Research/a.md")
        src = inputs.gather(
            iss,
            "researcher",
            DOCS,
            run=Gh({endpoint("Research/a.md"): NOT_FOUND, endpoint("Research"): NOT_FOUND}),
        )
        self.assertIsNone(src.docs[0].text)
        gh = Gh(
            {
                endpoint("Research/a.md"): ok(code=1, stderr="gh: Server Error (HTTP 502)\n"),
                endpoint("Research"): listing(),
            }
        )
        with self.assertRaisesRegex(
            RuntimeError, r"^gh api contents Research/a\.md: gh: Server Error \(HTTP 502\)$"
        ):
            inputs.gather(iss, "researcher", DOCS, run=gh)

    def test_links_order_dedupe_and_scope(self):
        a, b, c, d = (BASE + f"Research/{n}.md" for n in "abcd")
        description = lines(
            f"First {a} and [b]({b}) then <{a}>.",
            f"`{c}`",
            "",
            "## Comments",
            "* Researcher, 2026-09-01T10:00:00.000Z:",
            f"  > {d}",
        )
        links = (
            issues.Link("https://linear.app/t/issue/RES-4#comment-1"),
            issues.Link(c),
            issues.Link(a),
            issues.Link("https://github.com/other/repo/blob/main/e.md"),
            issues.Link(BASE + "Research/e.md"),
        )
        gh = Gh(
            {endpoint(f"Research/{n}.md"): ok(n) for n in "abce"}
            | {endpoint("Research"): listing()}
        )
        src = inputs.gather(issue(description, links=links), "researcher", DOCS, run=gh)
        self.assertEqual(
            [d.path for d in src.docs],
            ["Research/a.md", "Research/b.md", "Research/c.md", "Research/e.md"],
        )
        self.assertEqual(src.docs[2].url, c)
        self.assertNotIn(endpoint("Research/d.md"), [c[0][-1] for c in gh.calls])

    def test_fragment_and_query_links_resolve_to_the_bare_path_and_dedupe(self):
        d, e = BASE + "Research/a.md", BASE + "Research/b.md"
        description = (
            f"Lines {d}#L10 and [b]({e}?plain=1) and <{d}> and `{BASE}Research/c.md#section`."
        )
        links = (
            issues.Link(d + "#section"),
            issues.Link(e),
            issues.Link(BASE + "Research/d.md?plain=1#L3"),
        )
        gh = Gh(
            {endpoint(f"Research/{n}.md"): ok(n) for n in "abcd"}
            | {endpoint("Research"): listing()}
        )
        src = inputs.gather(issue(description, links=links), "researcher", DOCS, run=gh)
        self.assertEqual(
            [x.path for x in src.docs],
            ["Research/a.md", "Research/b.md", "Research/c.md", "Research/d.md"],
        )
        self.assertEqual([x.text for x in src.docs], list("abcd"))
        self.assertEqual(
            [x.url for x in src.docs],
            [
                d + "#L10",
                e + "?plain=1",
                BASE + "Research/c.md#section",
                BASE + "Research/d.md?plain=1#L3",
            ],
        )
        self.assertEqual(
            [c[0][-1] for c in gh.calls],
            [endpoint("Research")] + [endpoint(f"Research/{n}.md") for n in "abcd"],
        )

    def test_fragment_link_to_a_design_doc_is_found_for_the_prd_fallback_and_build(self):
        url = BASE + "Product%20Design/2026-08-01-PM-1-x.md#L10"
        gh = Gh(
            {
                endpoint("Product%20Design"): listing(),
                endpoint("Product%20Design/2026-08-01-PM-1-x.md"): ok("p1"),
            }
        )
        src = inputs.gather(issue(url, ident="PM-9"), "pm", DOCS, run=gh)
        self.assertEqual(
            (src.earlier.path, src.earlier.url, src.earlier.text),
            ("Product Design/2026-08-01-PM-1-x.md", url, "p1"),
        )
        gh = Gh({endpoint("Product%20Design/2026-08-01-PM-1-x.md"): ok("p1")})
        src = inputs.gather(
            issue("", links=(issues.Link(url),), ident="ENG-7"), "engineer", DOCS, run=gh
        )
        got = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            src,
            humans=HUMANS,
            target=target.Target("o", "r", "ENG-7-x"),
            docs=DOCS,
        )
        self.assertIn("## PRD: `Product Design/2026-08-01-PM-1-x.md`\n\n" + fenced("p1"), got)
        self.assertIn(f"Links: https://linear.app/t/issue/ENG-7, {url}\n", got)

    def test_comments_header_with_crlf(self):
        description = f"{BASE}Research/a.md\r\n## Comments\r\n  > {BASE}Research/b.md"
        gh = Gh({endpoint("Research/a.md"): ok("a"), endpoint("Research"): listing()})
        src = inputs.gather(issue(description), "researcher", DOCS, run=gh)
        self.assertEqual([d.path for d in src.docs], ["Research/a.md"])

    def test_earlier_version_listing_newest_match(self):
        names = (
            "2026-08-01-RES-4-old.md",
            "2026-09-05-RES-4-new.md",
            "2026-09-09-RES-40-other.md",
            "2026-09-09-RES-4-x.txt",
            "2026-09-07-RES-9-nope.md",
            "README.md",
        )
        gh = Gh(
            {
                endpoint("Research"): listing(*names),
                endpoint("Research/2026-09-05-RES-4-new.md"): ok("# New\n"),
            }
        )
        src = inputs.gather(issue(""), "researcher", DOCS, run=gh)
        self.assertEqual(
            src.earlier,
            inputs.Doc(
                BASE + "Research/2026-09-05-RES-4-new.md",
                "Research/2026-09-05-RES-4-new.md",
                "# New\n",
            ),
        )
        self.assertEqual(src.docs, ())
        self.assertEqual(gh.calls[0], (["gh", "api", endpoint("Research")], SHORT))
        self.assertEqual(gh.calls[1], (RAW + [endpoint("Research/2026-09-05-RES-4-new.md")], SHORT))

    def test_earlier_version_is_not_also_a_linked_doc(self):
        p = "Research/2026-09-05-RES-4-new.md"
        gh = Gh(
            {
                endpoint("Research"): listing("2026-09-05-RES-4-new.md"),
                endpoint(p): ok("n"),
                endpoint("Research/b.md"): ok("b"),
            }
        )
        src = inputs.gather(issue(f"{BASE}{p} {BASE}Research/b.md"), "researcher", DOCS, run=gh)
        self.assertEqual(src.earlier.path, p)
        self.assertEqual([d.path for d in src.docs], ["Research/b.md"])
        self.assertEqual([c[0][-1] for c in gh.calls].count(endpoint(p)), 1)

    def test_no_match_or_missing_dir_means_no_earlier_version(self):
        for res in (listing("2026-08-01-RES-9-x.md"), listing(), NOT_FOUND):
            with self.subTest(res=res.stdout or res.stderr):
                src = inputs.gather(
                    issue(""), "researcher", DOCS, run=Gh({endpoint("Research"): res})
                )
                self.assertIsNone(src.earlier)

    def test_listing_failure_and_bad_listing_raise(self):
        with self.assertRaisesRegex(RuntimeError, r"^gh api contents Research: boom$"):
            inputs.gather(
                issue(""),
                "researcher",
                DOCS,
                run=Gh({endpoint("Research"): ok(code=1, stderr="boom")}),
            )
        for bad in ("not json", json.dumps({"name": "x"}), json.dumps([1])):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(
                    RuntimeError, r"^gh api contents Research: unusable response$"
                ),
            ):
                inputs.gather(
                    issue(""), "researcher", DOCS, run=Gh({endpoint("Research"): ok(bad)})
                )

    def test_design_falls_back_to_first_design_link(self):
        prd1, prd2 = (
            BASE + "Product%20Design/2026-08-01-PM-1-x.md",
            BASE + "Product%20Design/2026-08-02-PM-2-y.md",
        )
        report = BASE + "Research/2026-09-01-RES-4-r.md"
        gh = Gh(
            {
                endpoint("Product%20Design"): listing("2026-08-01-PM-1-x.md"),
                endpoint("Product%20Design/2026-08-01-PM-1-x.md"): ok("p1"),
                endpoint("Product%20Design/2026-08-02-PM-2-y.md"): ok("p2"),
                endpoint("Research/2026-09-01-RES-4-r.md"): ok("r"),
            }
        )
        src = inputs.gather(issue(f"{report} {prd1} {prd2}", ident="PM-9"), "pm", DOCS, run=gh)
        self.assertEqual(src.earlier.path, "Product Design/2026-08-01-PM-1-x.md")
        self.assertEqual(src.earlier.url, prd1)
        self.assertEqual(
            [d.path for d in src.docs],
            ["Research/2026-09-01-RES-4-r.md", "Product Design/2026-08-02-PM-2-y.md"],
        )

    def test_design_listing_wins_over_link_fallback(self):
        prd = BASE + "Product%20Design/2026-08-01-PM-1-x.md"
        gh = Gh(
            {
                endpoint("Product%20Design"): listing("2026-09-01-PM-9-mine.md"),
                endpoint("Product%20Design/2026-09-01-PM-9-mine.md"): ok("mine"),
                endpoint("Product%20Design/2026-08-01-PM-1-x.md"): ok("p1"),
            }
        )
        src = inputs.gather(issue(prd, ident="PM-9"), "pm", DOCS, run=gh)
        self.assertEqual(src.earlier.path, "Product Design/2026-09-01-PM-9-mine.md")
        self.assertEqual(src.earlier.url, BASE + "Product%20Design/2026-09-01-PM-9-mine.md")
        self.assertEqual([d.path for d in src.docs], ["Product Design/2026-08-01-PM-1-x.md"])

    def test_no_listing_hit_and_no_design_link_of_a_design_role_means_no_earlier(self):
        for role, ident, listed, link, path in (
            ("pm", "PM-9", "Product%20Design", "Research/r.md", "Research/r.md"),
            ("researcher", "RES-4", "Research", "Product%20Design/p.md", "Product Design/p.md"),
        ):
            with self.subTest(role=role):
                gh = Gh({endpoint(listed): listing(), endpoint(link): ok("x")})
                src = inputs.gather(issue(f"{BASE}{link}", ident=ident), role, DOCS, run=gh)
                self.assertIsNone(src.earlier)
                self.assertEqual([d.path for d in src.docs], [path])

    def test_build_lists_nothing_and_keeps_every_link(self):
        prd = BASE + "Product%20Design/2026-09-01-PM-9-x.md"
        gh = Gh(
            {
                endpoint("Product%20Design/2026-09-01-PM-9-x.md"): ok("prd"),
                endpoint("Research/r.md"): ok("r"),
            }
        )
        src = inputs.gather(
            issue(f"{prd} {BASE}Research/r.md", ident="ENG-7"), "engineer", DOCS, run=gh
        )
        self.assertIsNone(src.earlier)
        self.assertEqual(
            [d.path for d in src.docs], ["Product Design/2026-09-01-PM-9-x.md", "Research/r.md"]
        )
        self.assertEqual(len(gh.calls), 2)

