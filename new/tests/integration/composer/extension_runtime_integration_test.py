"""A new runtime is a config value to the Composer: a local `runtime` and its [adapters.<name>] table, with no package
for it, resolve, compose and list the catalog in a child process (child.Case); no runtime text in the prompt."""

import os
import unittest

from agent_pm.role.composer.prompt import RETURN

from new.tests.case import ROOT
from new.tests.integration.composer import child

BANNER = "Extension handover."
LOCAL = (
    'runtime = "extension"\n\n[adapters.extension]\nimplementation = "adapters.extension:ExtensionAdapter"\n'
    f'tier = 4\nbanner = "{BANNER}"\n'
)


class ExtensionRuntime(child.Case):
    def test_its_runtime_resolves_composes_and_the_catalog_lists_every_role(self):
        self.local(LOCAL)
        got = self.compose("dummy-tester", "echo")
        self.assertFalse(got["repo_imported"])
        for path in got["files"]:
            self.assertTrue(path.startswith(ROOT + os.sep), path)
        self.assertEqual(
            (got["run"]["runtime"], got["run"]["tier"], got["run"]["effort"]),
            ("extension", 3, "low"),
        )
        self.assertNotIn(BANNER, got["prompt"])
        self.assertIn(f"\n{RETURN}\n", got["prompt"])
        self.assertIn("# Input\n\nHello.", got["prompt"])
        tiered = self.compose("dummy-tester", "echo", overlays=[{"tier": 4}])["run"]
        self.assertEqual((tiered["runtime"], tiered["tier"]), ("extension", 4))
        catalog = self.child("print(json.dumps(composer.catalog()))")
        self.assertEqual(list(catalog), ["researcher", "pm", "engineer", "dummy-tester"])
        self.assertIn("echo", catalog["dummy-tester"])


if __name__ == "__main__":
    unittest.main()
