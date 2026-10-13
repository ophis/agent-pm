"""Recovery behavior."""

import errno
import os
import shlex
import subprocess
from unittest import mock

from agent_pm.manager.engine import crew, roster

from manager.tests.case import PYTHONPATH
from manager.tests.unit.crew.case import RosterCase
from manager.tests.unit.crew.data import (
    HEADER,
    MGR,
    PY,
    SID,
    SID2,
    SessionView,
    entry,
    nl,
    recovered,
    result,
    row,
    started,
    tail,
)
from manager.tests.unit.crew.runtime import Api, Runs


class ResumeTest(RosterCase):
    RUN = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "errors": "replace",
    }

    def test_each_gone_entry_runs_its_recovery_argv_in_its_cwd_unlocked_no_shell_in_table_order(
        self,
    ):
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": f"{PYTHONPATH}:/u1"}))
        cwd, finished = os.path.join(self.dir, "it's a role"), os.path.join(self.dir, "finished")
        os.mkdir(finished)
        with open(os.path.join(finished, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "done"}))
        entries = {
            "b": entry(cwd="/w/b", resume=started("b", "--client", "rt1")),
            "a": entry(cwd=cwd, resume=started("a", "; rm -rf ~")),
            "B": entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
            "p1": entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--issue", "TASK-1"]),
            "f1": entry("pipeline", cwd=finished),
            "live": entry(sid=SID2),
        }
        self.seed(MGR, **entries)
        live = [row("live", SID2, view=SessionView(shown="mgr"))]
        rc, listed, err = self.main_with(Api(rows=live), "attach")
        self.assertEqual((rc, err, self.runs.recoveries), (0, "", []))
        self.runs = Runs(
            {
                "b": (0, f"tui: session b: tmux attach -t '=b'\nb {SID}\n"),
                "a": (0, "  a \n\n  \n"),
                "/x/drive.py": (0, "drive.py: session x\n"),
                "/x/router.py": (0, ""),
            },
            self.lock,
        )
        self.assertEqual(
            self.main_with(Api(rows=live), "attach", "--resume"),
            (
                0,
                listed
                + nl(
                    "resumed B: exit 0: drive.py: session x",
                    "resumed a: exit 0: a",
                    f"resumed b: exit 0: b {SID}",
                    "resumed p1: exit 0",
                ),
                "",
            ),
        )
        ran = {name: roster.recovery(entries[name]) for name in ("B", "a", "b", "p1")}
        env = {n: {"PYTHONPATH": f"{PYTHONPATH}:/u1"} if n in "ab" else {} for n in ran}
        self.assertEqual(
            self.runs.recoveries,
            [
                (
                    argv,
                    {"cwd": entries[n]["cwd"], "env": {**os.environ, **env[n]}, **self.RUN},
                    True,
                )
                for n, argv in ran.items()
            ],
        )
        printed = dict(
            line.removeprefix("resume ").split(": ", 1)
            for line in listed.splitlines()
            if line.startswith("resume ")
        )
        self.assertEqual(
            {n: shlex.split(cmd) for n, cmd in printed.items()},
            {
                **{
                    n: [*(["env", f"PYTHONPATH={env[n]['PYTHONPATH']}"] if env[n] else []), *argv]
                    for n, argv in ran.items()
                },
                "B": ["cd", cwd, "&&", *ran["B"]],
            },
        )

    def test_a_crew_recovery_gets_its_import_root_the_rest_the_users_pythonpath(self):
        entries = {
            "B": entry("role", cwd=self.dir),
            "b": entry(cwd=self.dir, resume=started("b")),
            "p1": entry("pipeline", cwd=self.dir),
        }
        for given, user in ((f"{PYTHONPATH}:/u1", "/u1"), (PYTHONPATH, None), (None, None)):
            with self.subTest(given=given), mock.patch.dict(os.environ):
                os.environ.pop("PYTHONPATH", None)
                if given is not None:
                    os.environ["PYTHONPATH"] = given
                self.seed(MGR, **entries)
                self.runs = Runs()
                self.assertEqual(self.main_with(Api(), "attach", "--resume")[0], 0)
                envs = {Runs.key(argv): kw["env"] for argv, kw, _ in self.runs.recoveries}
                self.assertEqual(
                    {k: env.get("PYTHONPATH") for k, env in envs.items()},
                    {
                        "/x/drive.py": user,
                        "b": PYTHONPATH if user is None else f"{PYTHONPATH}:{user}",
                        "/x/router.py": user,
                    },
                )
                with mock.patch.dict(os.environ, envs["b"], clear=True):
                    crew._drop_import_root()
                    self.assertEqual(os.environ.get("PYTHONPATH"), user)

    def test_a_failing_recovery_stops_none_and_attach_exits_1(self):
        refusal = "router.py: TASK-1 has a live agent run: tmux attach -t '=agent-pm-pm-TASK-1'"
        self.seed(
            MGR,
            a1=entry(resume=started("a1")),
            a2=entry(resume=started("a2")),
            a3=entry("role", sid=None),
            a4=entry(resume=started("a4")),
            p1=entry("pipeline", resume=[PY, "/x/router.py"]),
            w1=entry(resume=started("w1")),
        )
        self.runs = Runs(
            {
                "a1": (1, "workers: a1: session ended at once\n"),
                "a2": FileNotFoundError(errno.ENOENT, "No such file or directory"),
                "a4": ValueError("embedded null byte"),
                "/x/router.py": (1, refusal + "\n"),
            }
        )
        rc, out, err = self.main_with(Api(), "attach", "--resume")
        self.assertEqual((rc, err), (1, ""))
        self.assertTrue(
            out.endswith(
                tail(self.mdir)
                + nl(
                    "resumed a1: exit 1: workers: a1: session ended at once",
                    "resumed a2: not run: No such file or directory",
                    "resumed a3: not run: no sid",
                    "resumed a4: not run: ValueError: embedded null byte",
                    f"resumed p1: exit 1: {refusal}",
                    "resumed w1: exit 0",
                )
            ),
            out,
        )
        self.assertEqual(
            [Runs.key(argv) for argv, _, _ in self.runs.recoveries],
            ["a1", "a2", "a4", "/x/router.py", "w1"],
        )

    def test_one_failed_recovery_of_any_kind_exits_1(self):
        cases = {
            "exit 1": ((1, "boom\n"), "exit 1: boom"),
            "exit 255": ((255, ""), "exit 255"),
            "an OSError": (PermissionError(errno.EACCES, "denied"), "not run: denied"),
            "a ValueError": (
                ValueError("embedded null byte"),
                "not run: ValueError: embedded null byte",
            ),
            "a TypeError": (TypeError("bad argv"), "not run: TypeError: bad argv"),
            "no sid": (None, "not run: no sid"),
        }
        for why, (failure, line) in cases.items():
            with self.subTest(why):
                bad = entry("role", sid=None) if failure is None else entry(resume=started("bad"))
                self.seed(MGR, bad=bad, ok=entry(resume=started("ok")))
                self.runs = Runs({} if failure is None else {"bad": failure})
                rc, out, err = self.main_with(Api(), "attach", "--resume")
                self.assertEqual((rc, err), (1, ""))
                self.assertTrue(
                    out.endswith(
                        tail(self.mdir) + nl(f"resumed bad: {line}", "resumed ok: exit 0")
                    ),
                    out,
                )

    def test_a_waiting_run_is_left_alone(self):
        asking = os.path.join(self.dir, "asking")
        os.mkdir(asking)
        with open(os.path.join(asking, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "needs_input", "questions": ["Which?"]}))
        self.seed(
            MGR,
            r1=entry("role", cwd=asking),
            p1=entry("pipeline", cwd=asking),
            w1=entry(cwd=asking, resume=started("w1")),
        )
        rc, out, err = self.main_with(Api(), "attach", "--resume")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            out,
            HEADER
            + nl(
                f"p1\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                f"r1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                f"w1\tworker\t{SID}\tgone\t-\t{asking}\t-",
                f"resume w1: {recovered(started('w1', '--client', 'rt0'))}",
            )
            + tail(self.mdir)
            + "resumed w1: exit 0\n",
        )
        self.assertEqual(
            [argv for argv, _, _ in self.runs.recoveries], [started("w1", "--client", "rt0")]
        )
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()},
            {"p1": "waiting", "r1": "waiting", "w1": "gone"},
        )
