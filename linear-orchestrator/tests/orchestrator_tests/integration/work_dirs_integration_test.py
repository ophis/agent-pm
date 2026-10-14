"""Work-directory overlap rejection through real filesystem symlinks."""

import os

from agent_pm.linear_orchestrator.engine import config
from orchestrator_tests.case import Case
from orchestrator_tests.config_fixtures import BASE, ConfigFile


class WorkDirectories(ConfigFile, Case):
    def test_refused(self):
        real = os.path.realpath(config.ROOT)
        d = os.path.realpath(self.dir)
        link, clink = os.path.join(d, "link"), os.path.join(d, "clink")
        os.symlink(real, link)
        os.makedirs(os.path.join(d, "c"))
        os.symlink(os.path.join(d, "c"), clink)
        repo = f"work_dir must neither lie in nor contain the repo {real}: "

        def clone(path):
            return f'[local_clones]\n"ophis/x" = "{path}"\n'

        def in_clone(path):
            return f"work_dir must neither lie in nor contain local_clones.ophis/x {path}: "

        cases = [
            (f'work_dir = "{real}"\n', "", f"{repo}{real!r}"),
            (f'work_dir = "{real}/w"\n', "", f"{repo}{real + '/w'!r}"),
            (f'work_dir = "{link}/w"\n', "", f"{repo}{link + '/w'!r}"),
            (f'work_dir = "{os.path.dirname(real)}"\n', "", f"{repo}{os.path.dirname(real)!r}"),
            (f'work_dir = "{d}/c"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{d + '/c'!r}"),
            (f'work_dir = "{clink}"\n', clone(f"{d}/c/x"), f"{in_clone(d + '/c/x')}{clink!r}"),
            (f'work_dir = "{clink}/w"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{clink + '/w'!r}"),
            ('work_dir = "w"\n', "", "work_dir must be a printable absolute or ~ path: 'w'"),
            ("work_dir = 5\n", "", "work_dir must be a printable absolute or ~ path: 5"),
        ]
        for text, tail, msg in cases:
            with self.subTest(text=text, tail=tail):
                self.fails(f"linear-orchestrator/config.toml: {msg}", text + BASE + tail)
