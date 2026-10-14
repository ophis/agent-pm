"""Holder process lifetime, environment and signal forwarding with native flock."""

import contextlib
import errno
import json
import multiprocessing
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_pm.manager.engine.holder import Holder
from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held

from manager.tests.case import PY, Case, child_env
from utils.tests.sandbox import reap


def _hold_contender(directory, barrier, release, outcomes):
    barrier.wait(timeout=10)
    try:
        with Roster(directory).hold(str(os.getpid())) as lease:
            outcomes.put(("held", os.getpid(), lease.id, lease.generation))
            release.wait()
    except Held:
        outcomes.put(("refused", os.getpid(), None, None))


class HolderTest(Case):
    def setUp(self):
        self.directory = os.path.join(self.agent_pm, "managers", "mgr")
        self.roster = Roster(self.directory)

    def start(self, script, *options):
        process = subprocess.Popen(
            (
                *PY,
                "-m",
                "agent_pm.manager.cli",
                "hold",
                "--manager",
                "mgr",
                *options,
                "--",
                sys.executable,
                "-I",
                "-S",
                "-c",
                script,
            ),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            process_group=0,
        )
        self.addCleanup(self.finish, process)
        return process

    def finish(self, process, master=None):
        # An unreaped leader pins its pid, so its own process group cannot be reused.
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            if master is None:
                process.communicate(timeout=3)
            else:
                self.wait_terminal(process, master, timeout=3)
        except subprocess.TimeoutExpired:
            reap(process)
            if master is None:
                process.communicate(timeout=10)
            else:
                self.wait_terminal(process, master)

    def wait_terminal(self, process, master, *, timeout=10):
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if select.select((master,), (), (), 0.01)[0]:
                try:
                    output = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    output = b""
                if not output:
                    return process.wait(timeout=max(0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
        return process.wait()

    def wait_for(self, path, *, content=False):
        deadline = time.monotonic() + 10
        while not path.exists() or (content and not path.read_text()):
            if time.monotonic() >= deadline:
                self.fail(f"{path}: child did not become ready")
            time.sleep(0.01)

    def terminal(self, script):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen(
            (*PY, "-c", f"import os\nos.login_tty({slave})\n{script}"),
            env=child_env(),
            start_new_session=True,
            pass_fds=(slave,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.finish, process, master)
        return process, master

    def test_child_environment_replaces_inherited_identity_and_preserves_exit_code(self):
        env = child_env()
        env.update(
            AGENT_PM_MANAGER="inherited",
            AGENT_PM_HOLDER_ID="inherited",
            AGENT_PM_HOLDER_GENERATION="999",
            AGENT_PM_SID="must-scrub",
        )
        result = subprocess.run(
            (
                *PY,
                "-m",
                "agent_pm.manager.cli",
                "hold",
                "--manager",
                "mgr",
                "--session",
                "display-only",
                "--",
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import json,os,sys\n"
                "keys=('AGENT_PM_MANAGER','AGENT_PM_HOLDER_ID','AGENT_PM_HOLDER_GENERATION')\n"
                "values={k:os.environ[k] for k in keys}\n"
                "values['AGENT_PM_SID']=os.environ.get('AGENT_PM_SID')\n"
                "print(json.dumps(values)); sys.exit(7)",
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        received = json.loads(result.stdout)
        self.assertEqual(received["AGENT_PM_MANAGER"], "mgr")
        self.assertNotEqual(received["AGENT_PM_HOLDER_ID"], "inherited")
        self.assertEqual(received["AGENT_PM_HOLDER_GENERATION"], "1")
        self.assertIsNone(received["AGENT_PM_SID"])
        with self.roster.hold("next") as lease:
            self.assertEqual(lease.generation, 2)
            self.assertNotEqual(received["AGENT_PM_HOLDER_ID"], lease.id)

    def test_wrapper_holds_lock_until_child_exits(self):
        ready = Path(self.home, "ready")
        done = Path(self.home, "done")
        process = self.start(
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); "
            f"done=pathlib.Path({str(done)!r})\nwhile not done.exists(): time.sleep(.01)"
        )
        self.addCleanup(done.touch)
        self.wait_for(ready)
        with self.assertRaises(Held), self.roster.hold("challenger"):
            self.fail("live holder admitted another manager")
        done.touch()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        with self.roster.hold("challenger") as lease:
            self.assertEqual(lease.generation, 2)

    def test_simultaneous_native_processes_allow_exactly_one_holder(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(3)
        release = context.Event()
        outcomes = context.Queue()
        self.addCleanup(outcomes.join_thread)
        self.addCleanup(outcomes.close)

        def finish(process):
            if process.is_alive():
                process.terminate()
            process.join(3)
            if process.is_alive():
                process.kill()
                process.join(10)
            self.assertFalse(process.is_alive())

        contenders = [
            context.Process(
                target=_hold_contender, args=(self.directory, barrier, release, outcomes)
            )
            for _ in range(2)
        ]
        for process in contenders:
            process.start()
            self.addCleanup(finish, process)
        self.addCleanup(release.set)
        barrier.wait(timeout=10)
        results = [outcomes.get(timeout=10), outcomes.get(timeout=10)]
        self.assertCountEqual([result[0] for result in results], ["held", "refused"])
        winner = next(result for result in results if result[0] == "held")
        self.assertTrue(
            any(process.pid == winner[1] and process.is_alive() for process in contenders)
        )
        ledger = json.loads(Path(self.roster.path).read_text())
        self.assertEqual(ledger["holder"]["id"], winner[2])
        self.assertEqual(ledger["lease_gen"], winner[3])
        self.assertEqual(winner[3], 1)
        release.set()
        for process in contenders:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

    def test_child_cannot_keep_dead_wrappers_lock(self):
        ready = Path(self.home, "ready")
        done = Path(self.home, "done")
        process = self.start(
            f"import os,pathlib,time; pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
            f"done=pathlib.Path({str(done)!r})\nwhile not done.exists(): time.sleep(.01)"
        )
        self.addCleanup(done.touch)
        self.wait_for(ready, content=True)
        child_pid = int(ready.read_text())
        process.kill()
        process.wait(timeout=10)
        os.kill(child_pid, 0)
        with self.roster.hold("challenger") as lease:
            self.assertEqual(lease.generation, 2)
        done.touch()
        process.communicate(timeout=10)

    def test_forced_wrapper_takes_over_without_killing_old_command(self):
        ready = Path(self.home, "ready")
        done = Path(self.home, "done")
        old = self.start(
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); "
            f"done=pathlib.Path({str(done)!r})\nwhile not done.exists(): time.sleep(.01)"
        )
        self.addCleanup(done.touch)
        self.wait_for(ready)
        new = self.start("pass", "--force")
        stdout, stderr = new.communicate(timeout=10)
        self.assertEqual((new.returncode, stdout, stderr), (0, "", ""))
        self.assertIsNone(old.poll())
        with self.roster.hold("third") as lease:
            self.assertEqual(lease.generation, 3)
        done.touch()
        stdout, stderr = old.communicate(timeout=10)
        self.assertEqual((old.returncode, stdout, stderr), (0, "", ""))

    def test_interrupt_and_terminate_reach_child_and_wait_before_unlock(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                ready = Path(self.home, f"ready-{signum}")
                observed = Path(self.home, f"signal-{signum}")
                done = Path(self.home, f"done-{signum}")
                process = self.start(
                    "import pathlib,signal,time\n"
                    f"observed=pathlib.Path({str(observed)!r})\n"
                    "def handle(signum,frame): observed.write_text(str(signum))\n"
                    "signal.signal(signal.SIGINT,handle)\n"
                    "signal.signal(signal.SIGTERM,handle)\n"
                    f"pathlib.Path({str(ready)!r}).touch()\n"
                    f"done=pathlib.Path({str(done)!r})\n"
                    "while not done.exists(): time.sleep(.01)"
                )
                self.addCleanup(done.touch)
                self.wait_for(ready)
                process.send_signal(signum)
                self.wait_for(observed, content=True)
                self.assertEqual(observed.read_text(), str(signum))
                self.assertIsNone(process.poll())
                with self.assertRaises(Held), self.roster.hold("challenger"):
                    self.fail("signal released lease before child exit")
                done.touch()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))

    def test_spawn_failure_releases_lock_and_restores_signal_handlers(self):
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        with self.assertRaises(FileNotFoundError):
            Holder(self.roster).run((os.path.join(self.home, "missing-manager"),))
        for signum, handler in previous.items():
            self.assertEqual(signal.getsignal(signum), handler)
        with self.roster.hold("next") as lease:
            self.assertEqual(lease.generation, 2)

    def test_terminate_reaches_manager_descendants_in_owned_process_group(self):
        ready = Path(self.home, "descendant-ready")
        manager_signal = Path(self.home, "manager-signal")
        descendant_signal = Path(self.home, "descendant-signal")
        done = Path(self.home, "descendant-done")
        descendant = (
            "import pathlib,signal,time\n"
            "def handle(signum,frame):\n"
            f" pathlib.Path({str(descendant_signal)!r}).write_text(str(signum))\n"
            "signal.signal(signal.SIGTERM,handle)\n"
            f"pathlib.Path({str(ready)!r}).touch()\n"
            f"done=pathlib.Path({str(done)!r})\n"
            "while not done.exists(): time.sleep(.01)"
        )
        process = self.start(
            "import pathlib,signal,subprocess,time\n"
            "def handle(signum,frame):\n"
            f" pathlib.Path({str(manager_signal)!r}).write_text(str(signum))\n"
            "signal.signal(signal.SIGTERM,handle)\n"
            f"child=subprocess.Popen(({sys.executable!r},'-I','-S','-c',{descendant!r}))\n"
            f"done=pathlib.Path({str(done)!r})\n"
            "while not done.exists(): time.sleep(.01)\n"
            "child.wait()"
        )
        self.addCleanup(done.touch)
        self.wait_for(ready)
        process.terminate()
        self.wait_for(manager_signal, content=True)
        self.wait_for(descendant_signal, content=True)
        self.assertEqual(manager_signal.read_text(), str(signal.SIGTERM))
        self.assertEqual(descendant_signal.read_text(), str(signal.SIGTERM))
        done.touch()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))

    def test_terminal_foreground_child_gets_one_interrupt_and_foreground_restores(self):
        ready = Path(self.home, "terminal-ready")
        observed = Path(self.home, "terminal-signal")
        done = Path(self.home, "terminal-done")
        result = Path(self.home, "terminal-result")
        group_file = Path(self.home, "terminal-child-group")
        child = (
            "import json,os,pathlib,signal,sys,time\n"
            "count=0\n"
            "def handle(signum,frame):\n"
            " global count\n"
            " count+=1\n"
            f" pathlib.Path({str(observed)!r}).write_text(str(count))\n"
            "signal.signal(signal.SIGINT,handle)\n"
            f"pathlib.Path({str(group_file)!r}).write_text(str(os.getpgrp()))\n"
            "sys.stdin.readline()\n"
            f"pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpgrp(),os.tcgetpgrp(0)]))\n"
            f"done=pathlib.Path({str(done)!r})\n"
            "while not done.exists(): time.sleep(.01)"
        )
        driver = (
            "import json,os,pathlib\n"
            "from agent_pm.manager.engine.holder import Holder\n"
            "from agent_pm.manager.engine.roster import Roster\n"
            "before=os.tcgetpgrp(0)\n"
            f"rc=Holder(Roster({self.directory!r})).run(({sys.executable!r},'-I','-S','-c',{child!r}))\n"
            f"pathlib.Path({str(result)!r}).write_text(json.dumps([rc,before,os.tcgetpgrp(0)]))"
        )
        process, master = self.terminal(driver)
        self.addCleanup(done.touch)
        os.write(master, b"ready\n")
        self.wait_for(ready, content=True)
        child_group, foreground = json.loads(ready.read_text())
        self.assertEqual(child_group, foreground)
        self.assertNotEqual(child_group, process.pid)
        os.write(master, b"\x03")
        self.wait_for(observed, content=True)
        done.touch()
        self.assertEqual(self.wait_terminal(process, master), 0)
        self.assertEqual(observed.read_text(), "1")
        returncode, before, after = json.loads(result.read_text())
        self.assertEqual(returncode, 0)
        self.assertEqual(before, process.pid)
        self.assertEqual(after, before)

    def test_terminal_handoff_failure_terminates_child_and_releases_lease(self):
        result = Path(self.home, "handoff-result")
        group_file = Path(self.home, "handoff-child-group")
        child = (
            "import os,pathlib,time\n"
            f"pathlib.Path({str(group_file)!r}).write_text(str(os.getpgrp()))\n"
            "time.sleep(60)"
        )
        driver = (
            "import errno,json,pathlib,traceback\n"
            "from unittest import mock\n"
            "from agent_pm.manager.engine.holder import Holder\n"
            "from agent_pm.manager.engine.roster import Roster\n"
            "try:\n"
            " with mock.patch('os.tcsetpgrp',side_effect=OSError(errno.EIO,'handoff unavailable')):\n"
            f"  Holder(Roster({self.directory!r})).run(({sys.executable!r},'-I','-S','-c',{child!r}))\n"
            "except OSError as error:\n"
            " detail={'errno':error.errno,'trace':traceback.format_exc()}\n"
            f" pathlib.Path({str(result)!r}).write_text(json.dumps(detail))"
        )
        process, master = self.terminal(driver)
        self.assertEqual(self.wait_terminal(process, master), 0)
        detail = json.loads(result.read_text())
        self.assertEqual(detail["errno"], errno.EIO, detail["trace"])
        with self.roster.hold("next") as lease:
            self.assertEqual(lease.generation, 2)

    def test_command_required_before_creating_roster(self):
        with self.assertRaises(ValueError):
            Holder(self.roster).run(())
        self.assertFalse(os.path.exists(self.directory))
