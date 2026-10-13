from orchestrator_test_helpers.unit.inputs.fence_fixtures import Fence as FenceBase


class Fence1(FenceBase):
    def test_fence_is_longer_than_any_backtick_run_and_trailing_newlines_add_no_blank_lines(self):
        for text, fence, shown in (
            ("plain", "```", "plain"),
            ("a ``` b", "````", "a ``` b"),
            ("`` and ```` and `", "`````", "`` and ```` and `"),
            ("`x`", "```", "`x`"),
            ("a\n\n", "```", "a"),
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    self.render_doc(text).endswith(
                        f"### `Research/a.md`\n\n{fence}markdown\n{shown}\n{fence}"
                    )
                )
