"""utils/config_file.py: merge, read_config, read_project (levels, ownership, the one local file), unset."""

import copy
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.utils import config_file
from agent_pm.utils.config_file import merge, read_config, read_project, unset

from utils.tests.case import REPO, Case

LOCAL = "~/.agent-pm/local.toml"


class ConfigFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "config.toml"
        self.local = self.root / "local.toml"

    def test_merge_nested_tables_and_replace_other_values_without_changing_inputs(self):
        base = {"table": {"keep": 1, "change": {"x": 1}, "list": [1]}, "value": 1}
        overlay = {"table": {"change": {"y": 2}, "list": [2]}, "value": {"x": 3}}
        original = copy.deepcopy((base, overlay))
        self.assertEqual(
            merge(base, overlay),
            {"table": {"keep": 1, "change": {"x": 1, "y": 2}, "list": [2]}, "value": {"x": 3}},
        )
        self.assertEqual((base, overlay), original)

    def test_merge_adds_keys_replaces_a_table_by_a_value_and_merges_at_any_depth(self):
        base = {"a": 1, "s": "x", "u": {"k": 1}, "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}}}
        over = {"b": 2, "s": {"k": 1}, "u": 5, "t": {"y": 2, "l": [3], "n": {"q": 3}}}
        self.assertEqual(
            merge(base, over),
            {
                "a": 1,
                "b": 2,
                "s": {"k": 1},
                "u": 5,
                "t": {"x": 1, "y": 2, "l": [3], "n": {"p": 1, "q": 3}},
            },
        )

    def test_read_merges_an_explicit_overlay(self):
        self.path.write_text('value = 1\n[table]\nkeep = "base"\nchange = "base"\n')
        self.local.write_text('value = 2\n[table]\nchange = "local"\n')
        self.assertEqual(
            read_config(str(self.path), str(self.local)),
            {"value": 2, "table": {"keep": "base", "change": "local"}},
        )

    def test_read_expands_overlay_home_without_reading_user_config(self):
        self.path.write_text("value = 1\n")
        self.local.write_text("value = 2\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(read_config(str(self.path), "~/local.toml"), {"value": 2})

    def test_absent_or_directory_overlay_is_ignored(self):
        self.path.write_text("value = 1\n")
        for overlay in (self.local, self.root):
            with self.subTest(overlay=overlay):
                self.assertEqual(read_config(str(self.path), str(overlay)), {"value": 1})

    def test_missing_base_and_malformed_files_propagate(self):
        with self.assertRaises(FileNotFoundError):
            read_config(str(self.path), str(self.local))
        self.path.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            read_config(str(self.path), str(self.local))
        self.path.write_text("value = 1\n")
        self.local.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            read_config(str(self.path), str(self.local))


class Defaults(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.toml")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('users = ["a", "b"]\n[t]\nx = 1\ny = 1\n')

    def local(self, text, name="local.toml"):
        """`text` as this test's ~/.agent-pm/<name>."""
        with open(os.path.join(self.agent_pm, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_both_paths_are_required_and_devnull_merges_nothing(self):
        self.local('extra = "x"\n')
        for args in ((), (self.path,)):
            with self.subTest(args=args), self.assertRaises(TypeError):
                read_config(*args)
        self.assertEqual(
            read_config(self.path, os.devnull), {"users": ["a", "b"], "t": {"x": 1, "y": 1}}
        )

    def test_the_home_local_file_goes_on_top_and_the_old_ones_are_not_read(self):
        for old in (f"{name}.local.toml" for name in ("core", "core-new", "linear-new")):
            self.local('users = ["old"]\n', old)
        self.assertEqual(
            read_config(self.path, LOCAL), {"users": ["a", "b"], "t": {"x": 1, "y": 1}}
        )
        self.local('users = ["c"]\n[t]\ny = 2\n[t.n]\nz = 3\n')
        self.assertEqual(
            read_config(self.path, LOCAL), {"users": ["c"], "t": {"x": 1, "y": 2, "n": {"z": 3}}}
        )


SESSION = '[hosts]\ndefault = "tmux"\n[hosts.tmux]\nstatus_line = false\n'
DELEGATE = 'runtime = "a"\nshow = ""\nteam_dirs = []\n[roles.pm]\ntier = 2\n[roles.pm.output]\ntype = "local"\n'
ORCH = 'team = ""\nwork_dir = "~/w"\n[roles.pm]\naccount = ""\nmax_runs = 3\n[core.roles.pm]\ngate = "g"\n'
MANAGER = 'runtime = "m"\n'


class Projects(Case):
    """A temp root of session/, delegate/, linear-orchestrator/ and manager/ config.toml (utils has none) and this
    test's ~/.agent-pm/local.toml."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        for project, text in (
            ("session", SESSION),
            ("delegate", DELEGATE),
            ("linear-orchestrator", ORCH),
            ("manager", MANAGER),
        ):
            self.write(project, text)

    def write(self, project, text):
        os.makedirs(os.path.join(self.root, project), exist_ok=True)
        with open(os.path.join(self.root, project, "config.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, name, **kw):
        return read_project(name, root=self.root, **kw)

    def test_the_paths(self):
        self.assertEqual(config_file.LOCAL, "~/.agent-pm/local.toml")
        self.assertEqual(config_file.ROOT, REPO)
        self.assertEqual(
            config_file.LEVELS,
            (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",)),
        )

    def test_each_project_reads_the_levels_at_or_below_it_a_higher_key_winning(self):
        session = {"hosts": {"default": "tmux", "tmux": {"status_line": False}}}
        delegate = {
            **session,
            "runtime": "a",
            "show": "",
            "team_dirs": [],
            "roles": {"pm": {"tier": 2, "output": {"type": "local"}}},
        }
        orch = {
            **delegate,
            "team": "",
            "work_dir": "~/w",
            "roles": {"pm": {"tier": 2, "output": {"type": "local"}, "account": "", "max_runs": 3}},
            "core": {"roles": {"pm": {"gate": "g"}}},
        }
        self.assertEqual(self.read("utils"), session)
        self.assertEqual(self.read("session"), session)
        self.assertEqual(self.read("delegate"), delegate)
        self.assertEqual(self.read("linear-orchestrator"), orch)
        self.assertEqual(self.read("manager"), {**orch, "runtime": "m"})

    def test_local_goes_over_all_tables_merging_and_lists_replaced_whole(self):
        self.local(
            'runtime = "b"\nteam_dirs = ["~/t"]\nteam = "T"\n[hosts.tmux]\nstatus_line = true\n'
            '[roles.pm]\ntier = 1\naccount = "pm@x"\n'
        )
        found = self.read("linear-orchestrator")
        self.assertEqual(
            (found["runtime"], found["team_dirs"], found["team"], found["hosts"]["tmux"]),
            ("b", ["~/t"], "T", {"status_line": True}),
        )
        self.assertEqual(
            found["roles"]["pm"],
            {"tier": 1, "output": {"type": "local"}, "account": "pm@x", "max_runs": 3},
        )
        self.assertEqual(self.read("manager")["runtime"], "b")

    def test_a_project_takes_only_the_local_keys_owned_at_or_below_it(self):
        self.local(
            'team = "T"\nshow = "none"\n[hosts.tmux]\nstatus_line = true\n[core.roles.pm]\ncwd = "/c"\n'
            '[roles.pm]\naccount = "pm@x"\ncwd = "/p"\n[roles.new]\naccount = "n@x"\nmax_runs = 2\ntier = 3\n'
            '[roles.pm.output]\ntype = "github"\n'
        )
        session, delegate = self.read("session"), self.read("delegate")
        self.assertEqual(session["hosts"]["tmux"], {"status_line": True})
        self.assertEqual(set(session), {"hosts"})
        self.assertEqual(
            delegate["roles"],
            {"pm": {"tier": 2, "output": {"type": "github"}, "cwd": "/p"}, "new": {"tier": 3}},
        )
        self.assertEqual(
            (delegate["show"], "team" in delegate, "core" in delegate), ("none", False, False)
        )
        orch = self.read("linear-orchestrator")
        self.assertEqual(orch["roles"]["new"], {"account": "n@x", "max_runs": 2, "tier": 3})
        self.assertEqual(orch["core"]["roles"]["pm"], {"gate": "g", "cwd": "/c"})

    def test_own_is_the_projects_file_and_the_local_keys_it_owns(self):
        self.local(
            'team = "T"\nruntime = "b"\n[roles.pm]\naccount = "pm@x"\ncwd = "/p"\ntier = 1\nacount = "typo"\n'
            '[roles.new]\nmax_runs = 2\n[roles.pm.output]\ntype = "github"\n'
        )
        self.write("delegate", 'cwd = ""\n' + DELEGATE)
        self.assertEqual(
            self.read("linear-orchestrator", own=True),
            {
                "team": "T",
                "work_dir": "~/w",
                "roles": {
                    "pm": {"account": "pm@x", "max_runs": 3, "acount": "typo"},
                    "new": {"max_runs": 2},
                },
                "core": {"roles": {"pm": {"gate": "g"}}},
            },
        )
        found = self.read("delegate", own=True)
        self.assertEqual(
            found["roles"],
            {"pm": {"tier": 1, "output": {"type": "github"}, "cwd": "/p", "acount": "typo"}},
        )
        self.assertNotIn("hosts", found)
        self.assertEqual(self.read("manager", own=True), {"runtime": "b"})

    def test_a_local_top_level_key_no_project_has_is_refused_at_run_time(self):
        self.local('runtime = "b"\nbogus = 1\n')
        for name in ("session", "delegate", "manager"):
            with (
                self.subTest(name),
                self.assertRaisesRegex(
                    ValueError,
                    r"^~/.agent-pm/local.toml: unknown key 'bogus': no project's config.toml has it$",
                ),
            ):
                self.read(name)

    def test_a_new_table_under_a_known_one_is_taken_as_its_owners(self):
        self.local('[hosts.other]\nimplementation = "a.b:C"\n[adapters]\n')
        with self.assertRaisesRegex(ValueError, "unknown key 'adapters'"):
            self.read("session")
        self.local('[hosts.other]\nimplementation = "a.b:C"\n')
        self.assertEqual(self.read("session")["hosts"]["other"], {"implementation": "a.b:C"})

    def test_no_local_file_and_no_config_file_are_empty(self):
        os.remove(os.path.join(self.root, "manager", "config.toml"))
        self.assertEqual(self.read("manager"), self.read("linear-orchestrator"))
        self.assertEqual(
            self.read("utils"), {"hosts": {"default": "tmux", "tmux": {"status_line": False}}}
        )

    def test_a_name_that_is_not_a_project_is_refused(self):
        for name in ("core", "tests", "", "linear_orchestrator", "../delegate"):
            with (
                self.subTest(name),
                self.assertRaisesRegex(ValueError, f"project {name!r}: want one of"),
            ):
                self.read(name)

    def test_a_key_in_two_same_level_projects_is_refused(self):
        self.write("utils", "[hosts]\nx = 1\n")
        with self.assertRaisesRegex(
            ValueError, "session/config.toml: hosts is also a same-level project's"
        ):
            self.read("delegate")

    def test_path_stands_in_for_the_projects_own_file(self):
        other = os.path.join(self.root, "other.toml")
        with open(other, "w", encoding="utf-8") as f:
            f.write('runtime = "z"\nlanguage = "x"\n')
        self.local('language = "y"\n')
        found = self.read("delegate", path=other)
        self.assertEqual((found["runtime"], found["language"], "roles" in found), ("z", "y", False))

    def test_an_explicit_local_file(self):
        local = os.path.join(self.root, "mine.toml")
        with open(local, "w", encoding="utf-8") as f:
            f.write('runtime = "q"\n')
        self.assertEqual(self.read("delegate", local=local)["runtime"], "q")

    def test_malformed_files_propagate(self):
        self.local("x = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            self.read("delegate")


class Unset(Case):
    def test_missing_or_empty_required_keys_by_dotted_key_a_star_each_subtable(self):
        cfg = {
            "team": "",
            "key": "k",
            "states": {"a": "1", "b": ""},
            "roles": {"x": {"account": ""}, "y": {}},
        }
        self.assertEqual(
            unset(
                cfg, ("team", "key", "gone", "states.a", "states.b", "states.c", "roles.*.account")
            ),
            ["team", "gone", "states.b", "states.c", "roles.x.account", "roles.y.account"],
        )
        self.assertEqual(unset({}, ("roles.*.account",)), [])


if __name__ == "__main__":
    unittest.main()
