import os

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.config.data import BASE, PIPELINE, git
from orchestrator_test_helpers.unit.config.work_dir_fixtures import WorkDir as WorkDirBase


class WorkDir1(WorkDirBase):
    def test_unset_is_in_home(self):
        work = os.path.realpath(self.agent_pm)
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.work_dir, cfg.runs_dir, cfg.logs_dir, cfg.runs_log, cfg.run_dir("TASK-9")),
            (
                work,
                work + "/work",
                work + "/logs",
                work + "/logs/runs.jsonl",
                work + "/work/TASK-9",
            ),
        )
        home = os.path.dirname(work)
        self.fails(
            "linear-orchestrator/config.toml: work_dir must neither lie in nor contain "
            f"local_clones.ophis/x {home}: '~/.agent-pm'",
            PIPELINE + '[local_clones]\n"ophis/x" = "~"\n',
        )

    def test_set(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        self.assertEqual(
            self.load('work_dir = "~/d"\n' + PIPELINE).work_dir, os.path.join(home, "d")
        )
        for name in ("d-c", "c"):
            path = os.path.join(home, name)
            git("init", "-q", path)
            git(
                "-C",
                path,
                "remote",
                "add",
                "origin",
                f"https://github.com/ophis/{'x' if name == 'd-c' else 'y'}.git",
            )
        cfg = self.load(
            f'work_dir = "{home}/d"\n'
            + PIPELINE
            + f'[local_clones]\n"ophis/x" = "{home}/d-c"\n"ophis/y" = "{home}/c"\n'
        )
        self.assertEqual(cfg.work_dir, f"{home}/d")

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
