import os

from orchestrator_test_helpers.unit.config.data import HTTPS, PIPELINE, SLUG, git
from orchestrator_test_helpers.unit.config.local_clones_fixtures import (
    LocalClones as LocalClonesBase,
)


class LocalClones1(LocalClonesBase):
    def test_entries_stored_as_realpaths(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        git("init", "-q", real)
        git("-C", real, "remote", "add", "origin", HTTPS)
        cfg = self.load(
            f'work_dir = "{self.dir}/w"\n'
            + PIPELINE
            + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n'
        )
        self.assertEqual(cfg.local_clones, {SLUG: real})
