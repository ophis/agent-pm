import json
import subprocess
from unittest import mock

from orchestrator_test_helpers.unit.config.data import PIPELINE, SLUG
from orchestrator_test_helpers.unit.config.local_clones_runnable_fixtures import (
    LocalClonesRunnable as LocalClonesRunnableBase,
)


class LocalClonesRunnable1(LocalClonesRunnableBase):
    def test_each_entry_is_checked_and_a_bad_one_names_its_key(self):
        self.assertEqual(list(self.runs(SLUG, self.clone())), ["researcher", "pm", "engineer"])
        for name, path, reason in self.bad():
            with self.subTest(name):
                self.fails(
                    f"linear-orchestrator/config.toml: local_clones.{SLUG}: {reason}",
                    PIPELINE + f'[local_clones]\n"{SLUG}" = {json.dumps(path)}\n',
                )

    def test_no_entries_runs_no_git(self):
        with mock.patch.object(subprocess, "run") as clone_error:
            self.assertEqual(list(self.load(PIPELINE).roles), ["researcher", "pm", "engineer"])
        clone_error.assert_not_called()
