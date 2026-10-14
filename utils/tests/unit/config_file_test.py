"""
utils/config_file.py: load, merge, read_project (levels, ownership, the one local file), unset,
freeze and thaw.
"""

import copy
import importlib.util
import operator
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.utils import config_file
from agent_pm.utils.config_file import (
    ConfigFileError,
    freeze,
    load,
    merge,
    read_project,
    read_resources,
    thaw,
    unset,
)

from utils.tests.case import REPO, Case


class ConfigFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "config.toml"

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

    def test_load_reads_one_file_and_a_missing_or_malformed_one_raises(self):
        with self.assertRaises(FileNotFoundError):
            load(str(self.path))
        self.path.write_text('value = 1\n[table]\nkeep = "base"\n')
        self.assertEqual(load(str(self.path)), {"value": 1, "table": {"keep": "base"}})
        self.path.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            load(str(self.path))


SESSION = '[hosts]\ndefault = "tmux"\n[hosts.tmux]\nstatus_line = false\n'
DELEGATE = 'runtime = "a"\nshow = ""\nteam_dirs = []\n[roles.pm]\ntier = 2\n[roles.pm.output]\ntype = "local"\n'
ORCH = 'team = ""\nwork_dir = "~/w"\n[roles.pm]\naccount = ""\nmax_runs = 3\n[core.roles.pm]\ngate = "g"\n'
MANAGER = 'runtime = "m"\n'


class FrozenTables(Case):
    def test_freeze_is_a_read_only_deep_copy_its_lists_tuples(self):
        source = {"a": {"l": [1, {"b": 2}], "t": (3, [4])}, "n": 5}
        frozen = freeze(source)
        source["a"]["l"][1]["b"] = 9
        source["a"]["l"].append(6)
        source["c"] = 7
        self.assertEqual(frozen, {"a": {"l": (1, {"b": 2}), "t": (3, (4,))}, "n": 5})
        for table, key in ((frozen, "c"), (frozen["a"], "c"), (frozen["a"]["l"][1], "c")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(table, key, 1)
        self.assertFalse(hasattr(frozen["a"]["l"], "append"))
        self.assertEqual(freeze(frozen), frozen)

    def test_thaw_is_a_fresh_mutable_copy_lists_and_tuples_as_given(self):
        source = {"a": {"l": [1, [2]], "t": (3, [4])}}
        frozen = freeze(source)
        first, second = thaw(frozen), thaw(freeze(frozen))
        self.assertEqual((first, second), (source, source))
        self.assertEqual([type(first["a"][k]) for k in ("l", "t")], [list, tuple])
        self.assertEqual((type(first), type(first["a"]["t"][1])), (dict, list))
        first["a"]["l"].append(5)
        self.assertEqual(second, source)
        self.assertIsNot(thaw(source)["a"]["l"], source["a"]["l"])


class Projects(Case):
    """A temp root of session/, delegate/, linear-orchestrator/ and manager/ config.toml (utils has
    none) and this test's ~/.agent-pm/config.local.toml.
    """

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
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, name, **kw):
        return read_project(name, root=self.root, **kw)

    def test_the_paths(self):
        self.assertEqual(config_file.LOCAL, "~/.agent-pm/config.local.toml")
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
            '[session.hosts.tmux]\nstatus_line = true\n[delegate]\nruntime = "b"\nteam_dirs = ["~/t"]\n[delegate.roles.pm]\ntier = 1\n[linear_orchestrator]\nteam = "T"\n[linear_orchestrator.roles.pm]\naccount = "pm@x"\n[manager]\nruntime = "b"\n'
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
            '[session.hosts.tmux]\nstatus_line = true\n[delegate]\nshow = "none"\n[delegate.roles.pm]\ncwd = "/p"\n[delegate.roles.new]\ntier = 3\n[delegate.roles.pm.output]\ntype = "github"\n[linear_orchestrator]\nteam = "T"\n[linear_orchestrator.core.roles.pm]\ncwd = "/c"\n[linear_orchestrator.roles.pm]\naccount = "pm@x"\n[linear_orchestrator.roles.new]\naccount = "n@x"\nmax_runs = 2\n'
        )
        self.write("delegate", 'cwd = ""\n' + DELEGATE)
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
            '[delegate]\nruntime = "b"\n[delegate.roles.pm]\ncwd = "/p"\ntier = 1\n[delegate.roles.pm.output]\ntype = "github"\n[linear_orchestrator]\nteam = "T"\n[linear_orchestrator.roles.pm]\naccount = "pm@x"\n[linear_orchestrator.roles.new]\nmax_runs = 2\n[manager]\nruntime = "b"\n'
        )
        self.write("delegate", 'cwd = ""\n' + DELEGATE)
        self.assertEqual(
            self.read("linear-orchestrator", own=True),
            {
                "team": "T",
                "work_dir": "~/w",
                "roles": {
                    "pm": {"account": "pm@x", "max_runs": 3},
                    "new": {"max_runs": 2},
                },
                "core": {"roles": {"pm": {"gate": "g"}}},
            },
        )
        found = self.read("delegate", own=True)
        self.assertEqual(
            found["roles"],
            {"pm": {"tier": 1, "output": {"type": "github"}, "cwd": "/p"}},
        )
        self.assertNotIn("hosts", found)
        self.assertEqual(self.read("manager", own=True), {"runtime": "b"})

    def test_a_local_top_level_key_no_project_has_is_refused_at_run_time(self):
        self.local("[session]\nbogus = 1\n")
        for name in ("session", "delegate", "manager"):
            with (
                self.subTest(name),
                self.assertRaisesRegex(
                    ValueError,
                    (r"^~/.agent-pm/config.local.toml: unknown key 'bogus' in \[session\]$"),
                ),
            ):
                self.read(name)

    def test_a_new_table_under_a_known_one_is_taken_as_its_owners(self):
        self.local("[session.hosts.other]\nstatus_line = true\n[session.adapters]\nx = 1\n")
        with self.assertRaisesRegex(ValueError, "unknown key 'x'"):
            self.read("session")
        self.local("[session.hosts.other]\nstatus_line = true\n")
        self.assertEqual(self.read("session")["hosts"]["other"], {"status_line": True})

    def test_no_local_file_and_no_config_file_are_empty(self):
        os.remove(os.path.join(self.root, "manager", "config.toml"))
        self.assertEqual(self.read("manager"), self.read("linear-orchestrator"))
        self.assertEqual(
            self.read("utils"), {"hosts": {"default": "tmux", "tmux": {"status_line": False}}}
        )

    def test_a_missing_own_config_or_given_path_is_an_os_error(self):
        own = os.path.join(self.root, "delegate", "config.toml")
        os.remove(own)
        for name, kw in (("delegate", {}), ("session", {"path": own})):
            with self.subTest(name), self.assertRaisesRegex(FileNotFoundError, f"{own}: missing"):
                self.read(name, **kw)
        self.assertIn("work_dir", self.read("linear-orchestrator"))

    def test_a_directory_as_own_config_is_an_os_error(self):
        own = os.path.join(self.root, "delegate", "config.toml")
        os.remove(own)
        os.mkdir(own)
        with self.assertRaisesRegex(OSError, f"{own}: missing"):
            self.read("delegate")

    def test_a_new_empty_local_table_is_dropped(self):
        self.local("[delegate.roles.ghost]\n")
        self.assertNotIn("ghost", self.read("linear-orchestrator").get("roles", {}))
        self.assertNotIn("ghost", self.read("delegate").get("roles", {}))

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
        self.local('[delegate]\nlanguage = "y"\n')
        found = self.read("delegate", path=other)
        self.assertEqual((found["runtime"], found["language"], "roles" in found), ("z", "y", False))

    def test_an_explicit_local_file(self):
        local = os.path.join(self.root, "mine.toml")
        with open(local, "w", encoding="utf-8") as f:
            f.write('[delegate]\nruntime = "q"\n')
        self.assertEqual(self.read("delegate", local=local)["runtime"], "q")

    def test_malformed_files_propagate(self):
        self.local("x = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            self.read("delegate")


class Resources(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        for project, text in (
            ("session", SESSION),
            ("delegate", DELEGATE),
            ("linear-orchestrator", ORCH),
        ):
            folder = Path(self.root, project)
            folder.mkdir()
            (folder / "config.toml").write_text(text)
        self.package = "config_fixture_" + Path(self.root).name.replace("-", "_")
        for section, text in (
            ("session", SESSION),
            ("delegate", DELEGATE),
            ("linear_orchestrator", ORCH),
        ):
            folder = Path(self.root, self.package, section)
            folder.mkdir(parents=True)
            (folder / "__init__.py").write_text("")
            (folder / "config.toml").write_text(text)
        modules = {}
        for section in ("session", "delegate", "linear_orchestrator"):
            name = f"{self.package}.{section}"
            spec = importlib.util.spec_from_file_location(
                name, Path(self.root, self.package, section, "__init__.py")
            )
            modules[name] = importlib.util.module_from_spec(spec)
        self.enterContext(mock.patch("importlib.import_module", side_effect=modules.__getitem__))
        self.resources = [
            (f"{self.package}.{section}", "config.toml")
            for section in ("session", "delegate", "linear_orchestrator")
        ]

    def local(self, text):
        Path(self.agent_pm, "config.local.toml").write_text(text)

    def test_reads_resources_and_interleaves_each_local_section(self):
        self.local(
            '[delegate.roles.pm]\ntier = 1\n[linear_orchestrator.roles.pm]\ntier = 3\naccount = "pm@x"\n'
        )
        found = read_resources(iter(self.resources))
        self.assertEqual(
            found["roles"]["pm"],
            {"tier": 3, "output": {"type": "local"}, "account": "pm@x", "max_runs": 3},
        )
        self.assertEqual(read_resources(self.resources[:2])["roles"]["pm"]["tier"], 1)
        self.assertEqual(found["hosts"]["default"], "tmux")

    def test_upper_packaged_values_override_lower_local_values(self):
        self.local('[delegate.roles.pm]\naccount = "not-known-at-this-layer"\n')
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'account'"):
            read_resources(self.resources)
        self.local('[delegate]\nruntime = "lower-local"\n[delegate.roles.pm]\ntier = 1\n')
        Path(self.root, self.package, "linear_orchestrator", "config.toml").write_text(
            ORCH.replace('account = ""', 'tier = 3\naccount = ""')
        )
        self.assertEqual(read_resources(self.resources)["roles"]["pm"]["tier"], 3)
        self.assertEqual(read_resources(self.resources)["runtime"], "lower-local")

    def test_only_supplied_layers_are_validated(self):
        self.local(
            "[session.hosts.tmux]\nstatus_line = true\n[delegate]\nanything = 1\n[linear_orchestrator]\nunknown = true\n"
        )
        self.assertTrue(read_resources(self.resources[:1])["hosts"]["tmux"]["status_line"])
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'anything'"):
            read_resources(self.resources[:2])
        self.local("[session]\nunknown = 1\n")
        with self.assertRaisesRegex(ConfigFileError, "unknown key 'unknown'"):
            read_resources(self.resources[:1])

    def test_path_replaces_one_resource_with_defaults_as_schema(self):
        override = Path(self.root, "override.toml")
        override.write_text('runtime = "override"\nnew_key = "default"\n')
        self.local('[delegate]\nnew_key = "local"\n[delegate.roles.pm]\ntier = 1\n')
        found = read_resources(
            iter(self.resources[:2]), paths={self.resources[1][0]: str(override)}
        )
        self.assertEqual(
            (found["runtime"], found["new_key"], found["roles"]["pm"]),
            ("override", "local", {"tier": 1}),
        )
        with self.assertRaises(FileNotFoundError):
            read_resources(
                self.resources[:2], paths={self.resources[1][0]: str(override) + ".missing"}
            )
        with self.assertRaisesRegex(ConfigFileError, "no resource"):
            read_resources(self.resources[:1], paths={"absent": str(override)})

    def test_flat_local_keys_name_the_required_section(self):
        for text, section in (
            ('runtime = "x"\n', "delegate"),
            ("[hosts.tmux]\nstatus_line = true\n", "session"),
        ):
            self.local(text)
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ConfigFileError, rf"move it under \[{section}\]"),
            ):
                read_resources(self.resources)
            with self.assertRaisesRegex(ConfigFileError, rf"move it under \[{section}\]"):
                read_project("linear-orchestrator", root=self.root)

    def test_unknown_nested_keys_and_non_table_sections_are_errors(self):
        for text in ("[delegate.roles.pm]\nunknown_field = 1\n", "session = 1\n"):
            self.local(text)
            with self.subTest(text=text), self.assertRaises(ConfigFileError):
                read_resources(self.resources)

    def test_package_location_wins_over_legacy_location(self):
        folder = Path(self.root, "session", "src", "agent_pm", "session")
        folder.mkdir(parents=True)
        (folder / "config.toml").write_text('[hosts]\ndefault = "packaged"\n')
        self.assertEqual(read_project("session", root=self.root)["hosts"], {"default": "packaged"})
        (folder / "config.toml").unlink()
        self.assertEqual(read_project("session", root=self.root)["hosts"]["default"], "tmux")


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
