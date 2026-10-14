"""The complete old prompt bytes remain fixed across the Composer transition."""

import json

from agent_pm.delegate.composer import Composer, render, resolve

from delegate.tests.case import Case
from delegate.tests.integration.composer.corpus import ORACLE, corpus, supplied_text


class PromptCorpus(Case):
    def test_composer_prompt_bytes(self):
        cfg, found, files, cases = corpus()
        expected = json.loads(ORACLE.read_text(encoding="utf-8"))
        self.assertEqual(set(cases), set(expected))
        composition = Composer((), cfg, team=found)
        with supplied_text(files):
            for key, (role, task, overlays, params) in cases.items():
                with self.subTest(case=key):
                    self.assertEqual(
                        composition.render(
                            composition.resolve(role, task, overlays=overlays), params
                        ).encode("utf-8"),
                        expected[key].encode("utf-8"),
                    )

    def test_pre_composer_prompt_bytes(self):
        cfg, found, files, cases = corpus()
        expected = json.loads(ORACLE.read_text(encoding="utf-8"))
        self.assertEqual(set(cases), set(expected))
        with supplied_text(files):
            for key, (role, task, overlays, params) in cases.items():
                with self.subTest(case=key):
                    run = resolve(role, task, overlays=overlays, team=found, cfg=cfg)
                    self.assertEqual(
                        render(run, params, team=found).encode("utf-8"),
                        expected[key].encode("utf-8"),
                    )
