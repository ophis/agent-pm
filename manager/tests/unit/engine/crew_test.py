"""Crew starts and controls Records through real fake-ctl processes, never lower-layer APIs."""

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import mock

from agent_pm.manager.engine.crew import BIN, HERE, Crew
from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held, Spec, State
from agent_pm.utils import Status

from manager.tests.case import Case
from utils.tests.fakes import ctl


class CrewTest(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.directory = os.path.join(self.home, "roster")
        self.roster = Roster(self.directory)
        self.crew = Crew(self.roster)
        with self.listed():
            self.crew.attach()

    def start(self, name, *, note=None, display=None):
        argv = (self.command, "start", name)
        if display is not None:
            argv += ("--display", display)
        return self.crew.run(Spec(name, argv, note))

    def gone(self, name):
        record = self.start(name)
        ctl.set_session(self.ctl_dir, name, alive=False)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.roster.entries()[name].state, State.GONE)
        return record

    def calls(self, verb):
        return [call for call in ctl.calls(self.ctl_dir) if call["argv"][0] == verb]

    def age(self, name, seconds=31):
        path = os.path.join(self.directory, "roster.json")
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        value["entries"][name]["started"] = (
            datetime.now(UTC) - timedelta(seconds=seconds)
        ).isoformat()
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream)

    def test_run_commits_the_last_line_and_keeps_intent_note_and_display(self):
        record = self.start("worker", note="investigate", display="opaque value")
        entry = self.roster.entries()["worker"]
        self.assertEqual(entry.record, record)
        self.assertEqual(entry.record.sessions, ("worker",))
        self.assertEqual(entry.record.resume, (self.command, "resume", "worker"))
        self.assertEqual(entry.record.display, "opaque value")
        self.assertEqual((entry.state, entry.note), (State.LIVE, "investigate"))

    def test_start_process_observes_starting_entry_unlocked_events_and_current_bin(self):
        record = self.fake_record("worker")
        code = "\n".join(
            (
                "import fcntl,json,os",
                f"path={self.directory!r}",
                "with open(path+'/roster.json') as f: entry=json.load(f)['entries']['worker']",
                "assert entry['state']=='starting' and entry['record'] is None",
                "fd=os.open(path+'/roster.lock',os.O_RDWR)",
                "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                "assert os.environ['AGENT_PM_EVENTS']==path+'/events'",
                f"assert os.environ['PATH'].split(os.pathsep)[0]=={BIN!r}",
                "print('starter diagnostics')",
                f"print({record.to_line()!r})",
            )
        )
        self.assertEqual(self.crew.run(Spec("worker", (sys.executable, "-c", code))), record)

    def test_bad_empty_nonzero_and_missing_command_runs_become_lost(self):
        cases = {
            "empty": (sys.executable, "-c", "pass"),
            "invalid": (sys.executable, "-c", "print('{}')"),
            "failed": (sys.executable, "-c", "import sys;sys.exit(7)"),
            "missing": ("missing-starter-task371",),
        }
        for name, argv in cases.items():
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.crew.run(Spec(name, argv))
            self.assertEqual(self.roster.entries()[name].state, State.LOST)
            with self.assertRaisesRegex(RuntimeError, "lost.*no Record"):
                self.crew.resume(name)
        with self.listed():
            table = self.crew.attach()
        for name in cases:
            self.assertIn(f"{name}\t-\t-\tlost\t", table)

    def test_timeout_marks_run_lost_and_resume_gone(self):
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == HERE:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise subprocess.TimeoutExpired(argv, options["timeout"])

        with (
            mock.patch("subprocess.run", side_effect=run),
            self.assertRaisesRegex(RuntimeError, "30 seconds"),
        ):
            self.crew.run(Spec("lost", (self.command, "start", "lost")))
        self.assertEqual(self.roster.entries()["lost"].state, State.LOST)
        with mock.patch("subprocess.run", side_effect=real_run):
            self.gone("gone")
        resume = (self.command, "resume", "gone")

        def timeout_resume(argv, **options):
            if tuple(argv) == resume:
                raise subprocess.TimeoutExpired(argv, options["timeout"])
            return real_run(argv, **options)

        with (
            mock.patch("subprocess.run", side_effect=timeout_resume),
            self.assertRaisesRegex(RuntimeError, "30 seconds"),
        ):
            self.crew.resume("gone")
        self.assertEqual(self.roster.entries()["gone"].state, State.GONE)

    def test_attach_batches_twenty_records_and_shows_only_displayable_live_entries(self):
        records = []
        for number in range(20):
            name = f"role{number}"
            records.append(self.start(name))
            ctl.set_session(self.ctl_dir, name, displayable=number < 2)
        with self.listed(records), mock.patch("subprocess.run", wraps=subprocess.run) as run:
            table = self.crew.attach()
        self.assertEqual(len(self.calls("status")), 1)
        self.assertEqual(len(self.calls("status")[0]["names"]), 20)
        self.assertEqual(self.calls("show")[0]["names"], ["role0", "role1"])
        self.assertEqual(
            len([call for call in run.call_args_list if tuple(call.args[0]) == HERE]), 1
        )
        self.assertIn("role0\tfake\t-\tworking", table)
        self.assertIn("role19\tfake\t-\tworking", table)

    def test_attach_renders_its_status_snapshot_during_a_concurrent_launch(self):
        self.start("old")
        with self.listed():
            real_run = subprocess.run

            def run(argv, **options):
                result = real_run(argv, **options)
                if tuple(argv) == (self.command, "status"):
                    self.start("new")
                return result

            with mock.patch("subprocess.run", side_effect=run):
                table = self.crew.attach()
        self.assertIn("old\tfake", table)
        self.assertNotIn("new\tfake", table)
        self.assertEqual(set(self.roster.entries()), {"old", "new"})

    def test_attach_maps_outcomes_and_keeps_terminal_states_stable(self):
        for name, outcome in (
            ("done", "done"),
            ("failed", "failed"),
            ("waiting", "needs_input"),
            ("gone", None),
        ):
            self.start(name)
            ctl.set_session(self.ctl_dir, name, alive=False, outcome=outcome)
        with self.listed():
            self.crew.attach()
        self.assertEqual(
            {name: entry.state for name, entry in self.roster.entries().items()},
            {
                "done": State.DONE,
                "failed": State.DONE,
                "waiting": State.WAITING,
                "gone": State.GONE,
            },
        )
        for name in ("done", "failed", "waiting"):
            ctl.set_session(self.ctl_dir, name, alive=True, outcome=None, displayable=True)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.calls("show"), [])
        self.assertEqual(self.roster.entries()["waiting"].state, State.WAITING)
        self.assertEqual(self.roster.entries()["done"].state, State.DONE)

    def test_manual_resume_is_detected_without_crew_starting_anything(self):
        self.gone("worker")
        ctl.set_session(self.ctl_dir, "worker", alive=True)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)
        self.assertEqual(self.calls("resume"), [])

    def test_attach_times_out_old_starting_and_resuming_entries_but_leaves_young_ones(self):
        self.roster.begin(Spec("old", (self.command, "start", "old")))
        self.roster.begin(Spec("young", (self.command, "start", "young")))
        self.age("old")
        self.gone("resuming")
        self.roster.claim_resume("resuming")
        self.age("resuming")
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.roster.entries()["old"].state, State.LOST)
        self.assertEqual(self.roster.entries()["young"].state, State.STARTING)
        self.assertEqual(self.roster.entries()["resuming"].state, State.GONE)

    def test_attach_adopts_orphan_records_and_retains_exact_name_lost_note(self):
        self.roster.begin(Spec("worker", (self.command, "start", "worker"), "keep"))
        self.age("worker")
        orphan = self.fake_record("worker")
        extra = self.fake_record("extra")
        ctl.set_session(self.ctl_dir, "worker", alive=True)
        ctl.set_session(self.ctl_dir, "extra", alive=True)
        with self.listed((orphan, orphan, extra)):
            self.crew.attach()
        self.assertEqual(set(self.roster.entries()), {"worker", "extra"})
        self.assertEqual(self.roster.entries()["worker"].note, "keep")
        self.assertEqual(self.roster.entries()["worker"].record, orphan)
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)

    def test_resume_replays_exact_argv_and_preserves_display(self):
        self.start("role", display="opaque")
        ctl.set_session(self.ctl_dir, "role", alive=False)
        with self.listed():
            table = self.crew.attach()
        self.assertIn("resume role: fake-ctl resume role", table)
        record = self.crew.resume("role")
        self.assertEqual(record.display, "opaque")
        self.assertEqual(self.calls("resume"), [{"argv": ["resume", "role"], "names": ["role"]}])
        self.assertEqual(self.roster.entries()["role"].state, State.LIVE)

    def test_resume_live_and_in_progress_start_nothing(self):
        record = self.start("live")
        self.assertEqual(self.crew.resume("live"), record)
        self.gone("resuming")
        self.roster.claim_resume("resuming")
        self.assertIsNone(self.crew.resume("resuming"))
        self.assertEqual(self.calls("resume"), [])

    def test_concurrent_resume_starts_once(self):
        self.gone("worker")

        def resume():
            return Crew(Roster(self.directory)).resume("worker")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resume(), range(2)))
        self.assertTrue(any(record is not None for record in results))
        self.assertEqual(len(self.calls("resume")), 1)
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)

    def test_status_returns_aliases_batches_and_never_shows_or_writes_state(self):
        self.start("one")
        self.start("two")
        ctl.set_session(self.ctl_dir, "one", displayable=True)
        ctl.set_session(self.ctl_dir, "two", alive=False, outcome="done")
        statuses = self.crew.status(("two", "one"))
        self.assertEqual([status.name for status in statuses], ["two", "one"])
        self.assertEqual(statuses[0].outcome, "done")
        self.assertEqual(len(self.calls("status")), 1)
        self.assertEqual(self.calls("show"), [])
        self.assertEqual(self.roster.entries()["two"].state, State.LIVE)

    def test_status_rejects_missing_duplicate_and_extra_responses(self):
        self.start("worker")
        responses = (
            "",
            (
                Status("worker", alive=True, state="working", outcome=None, attach=None).to_line()
                + "\n"
            )
            * 2,
            Status("extra", alive=True, state="working", outcome=None, attach=None).to_line()
            + "\n",
        )
        for raw in responses:
            with (
                self.subTest(raw=raw),
                mock.patch(
                    "subprocess.run", return_value=subprocess.CompletedProcess([], 0, raw, "")
                ),
                self.assertRaises(RuntimeError),
            ):
                self.crew.status()

    def test_stop_by_session_closes_then_forgets_and_forget_does_not_close(self):
        record = self.start("one")
        self.start("two")
        self.crew.stop(record.sessions[0])
        ctl.set_session(self.ctl_dir, "two", alive=False)
        self.crew.forget("two")
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.calls("close"), [{"argv": ["close"], "names": ["one"]}])

    def test_failed_close_retains_entry(self):
        self.start("worker")
        with (
            mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "close failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "close failed"),
        ):
            self.crew.stop("worker")
        self.assertIn("worker", self.roster.entries())

    def test_stop_all_includes_here_orphans_deduplicates_and_excludes_unlisted_records(self):
        owned = self.start("owned")
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        ctl.set_session(self.ctl_dir, "outside", alive=True)
        with self.listed((owned, orphan)):
            self.assertEqual(self.crew.stop_all(dry_run=True), ["orphan", "owned"])
        self.assertEqual(self.calls("close"), [])
        with self.listed((owned, orphan)):
            self.assertEqual(self.crew.stop_all(), ["orphan", "owned"])
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.calls("close"), [{"argv": ["close"], "names": ["owned", "orphan"]}])

    def test_each_ctl_is_queried_once_and_answers_are_mapped_to_their_names(self):
        self.start("one")
        second = "other-ctl"
        shutil.copyfile(
            os.path.join(self.bin_dir, self.command), os.path.join(self.bin_dir, second)
        )
        os.chmod(os.path.join(self.bin_dir, second), 0o700)
        self.roster.begin(Spec("two", (second, "start", "two")))
        self.roster.commit("two", replace(self.fake_record("two"), ctl=(second,)))
        ctl.set_session(self.ctl_dir, "two", alive=True)
        statuses = self.crew.status()
        self.assertEqual([status.name for status in statuses], ["one", "two"])
        self.assertEqual([call["names"] for call in self.calls("status")], [["one"], ["two"]])

    def test_a_live_holder_blocks_attach_and_launch_without_starting_a_process(self):
        owner = self.fake_record("owner")
        held = Crew(Roster(self.directory), owner="owner")
        ctl.set_session(self.ctl_dir, "owner", alive=True)
        with self.listed((owner,)):
            held.attach()
            other = Crew(Roster(self.directory), owner="other")
            with self.assertRaises(Held):
                other.attach()
            with self.assertRaises(Held):
                other.run(Spec("worker", (self.command, "start", "worker")))
        self.assertEqual(self.calls("start"), [])
        self.assertNotIn("worker", self.roster.entries())

    def test_failed_resume_restores_gone_and_keeps_previous_record(self):
        original = self.gone("worker")
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == original.resume:
                return subprocess.CompletedProcess(argv, 1, "", "cannot resume")
            return real_run(argv, **options)

        with (
            mock.patch("subprocess.run", side_effect=run),
            self.assertRaisesRegex(RuntimeError, "cannot resume"),
        ):
            self.crew.resume("worker")
        entry = self.roster.entries()["worker"]
        self.assertEqual((entry.state, entry.record), (State.GONE, original))

    def test_forget_refuses_live_record_without_closing_or_removing_it(self):
        self.start("worker")
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.crew.forget("worker")
        self.assertIn("worker", self.roster.entries())
        self.assertEqual(self.calls("close"), [])

    def test_note_updates_and_release_allows_new_manager_to_attach(self):
        self.start("worker", note="before")
        self.crew.note("worker", "after")
        self.assertEqual(self.roster.entries()["worker"].note, "after")
        self.crew.release()
        other = Crew(Roster(self.directory), owner="new")
        with self.listed():
            other.attach()

    def test_next_event_returns_only_the_event_with_its_line(self):
        with open(self.roster.events, "a", encoding="utf-8") as stream:
            stream.write("12:00:00 worker outcome done\n")
        self.assertEqual(self.crew.next_event(after=0, gen=0), (1, "12:00:00 worker outcome done"))
