"""A new client is a config value to the Composer: a local `client` and its [adapters.<name>]
table, with no package for it, resolve, compose and list the catalog in a child process
(child.Case); no client text in the prompt.
"""

import os
import unittest

from agent_pm.delegate.composer.prompt import RETURN

from delegate.tests.case import ROOTS
from delegate.tests.integration.composer import child

BANNER = "Extension handover."
LOCAL = (
    '[delegate]\nclient = "extension"\n\n[session.adapters.extension]\nimplementation = "extension_client:ExtensionAdapter"\n'
    f'tier = 4\nbanner = "{BANNER}"\n'
)


class ExtensionClient(child.Case):
    def test_its_client_resolves_composes_and_the_catalog_lists_every_role(self):
        self.local(LOCAL)
        got = self.compose("dummy-tester", "echo")
        self.assertFalse(got["repo_imported"])
        for path in got["files"]:
            self.assertTrue(path.startswith(tuple(r + os.sep for r in ROOTS)), path)
        self.assertEqual(
            (got["run"]["client"], got["run"]["tier"], got["run"]["effort"]),
            ("extension", 3, "low"),
        )
        self.assertNotIn(BANNER, got["prompt"])
        self.assertIn(f"\n{RETURN}\n", got["prompt"])
        self.assertIn("# Input\n\nHello.", got["prompt"])
        tiered = self.compose("dummy-tester", "echo", overlays=[{"tier": 4}])["run"]
        self.assertEqual((tiered["client"], tiered["tier"]), ("extension", 4))
        catalog = self.child("print(json.dumps(Composer().catalog()))")
        self.assertEqual(list(catalog), ["researcher", "pm", "engineer", "dummy-tester"])
        self.assertIn("echo", catalog["dummy-tester"])


if __name__ == "__main__":
    unittest.main()
