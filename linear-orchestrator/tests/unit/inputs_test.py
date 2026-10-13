from agent_pm.linear_orchestrator.engine import config, inputs
from orchestrator_test_helpers.unit.inputs.data import BASE, DOCS
from orchestrator_test_helpers.unit.inputs.doc_path_fixtures import DocPath as DocPathBase


class DocPath1(DocPathBase):
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
