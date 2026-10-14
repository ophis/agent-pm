import json
import os
import sys

from tests.case import REPO
from tests.guard_checks import exports
from tests.guard_data import OBJECT_EXPORTS_ALLOWED
from tests.guard_data.delegate import DELEGATE_INTERFACE, DELEGATE_INTERFACE_MODULES
from tests.guard_data.session import SESSION_INTERFACE, SESSION_INTERFACE_MODULES
from tests.guard_samples import PKG, Sample


class EventObjectExports(Sample):
    EVENT = "delegate/src/agent_pm/delegate/event/"

    def exports(self, root):
        return [entry for entry in exports.object_exports(root) if entry.startswith(self.EVENT)]

    def test_seven_callable_or_mutated_export_bypasses_fail(self):
        for sample in (
            "import functools\ndef f(): pass\naction = functools.partial(f)\n__all__ = ['action']\n",
            "action = staticmethod(lambda: None)\n__all__ = ['action']\n",
            "def f(): pass\naction = f if True else f\n__all__ = ['action']\n",
            "import os\naction = getattr(os, 'getcwd')\n__all__ = ['action']\n",
            "def f(): pass\n__all__ = []\n__all__ += ['f']\n",
            "def f(): pass\n__all__ = []\n__all__.append('f')\n",
            "def f(): pass\n__all__ = []\nif True:\n    __all__ = ['f']\n",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(self.exports(self.tree({self.EVENT + "__init__.py": sample})))

    def test_indexed_export_mutations_are_rejected(self):
        for mutation in (
            "__all__[0] = 'f'",
            "__all__[:] = ['f']",
            "__all__[0] += 'f'",
            "del __all__[0]",
        ):
            with self.subTest(mutation=mutation):
                source = "def f(): pass\nCONSTANT = 1\n__all__ = ['CONSTANT']\n" + mutation
                root = self.tree({self.EVENT + "__init__.py": source})
                self.assertEqual(
                    self.exports(root), [self.EVENT + "__init__.py: no literal __all__"]
                )

    def test_runtime_export_writes_are_rejected(self):
        for mutation in (
            "globals()['__all__'] = ['f']",
            "globals()['__all__'] += ['f']",
            "globals()['__all__'].append('f')",
            "del globals()['__all__']",
            "setattr(sys.modules[__name__], '__all__', ['f'])",
            "builtins.setattr(sys.modules[__name__], '__all__', ['f'])",
            "write(sys.modules[__name__], '__all__', ['f'])",
            "delattr(sys.modules[__name__], '__all__')",
            "sys.modules[__name__].__all__ = ['f']",
            "sys.modules[__name__].__all__ += ['f']",
            "sys.modules[__name__].__all__.append('f')",
            "del sys.modules[__name__].__all__",
        ):
            for source in (mutation, "def mutate():\n    " + mutation):
                with self.subTest(source=source):
                    root = self.tree(
                        {
                            self.EVENT + "__init__.py": (
                                "import sys, builtins\nfrom builtins import setattr as write\n"
                                "def f(): pass\nCONSTANT = 1\n__all__ = ['CONSTANT']\n" + source
                            )
                        }
                    )
                    self.assertEqual(
                        self.exports(root), [self.EVENT + "__init__.py: no literal __all__"]
                    )

    def test_runtime_export_reads_are_allowed(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "import sys\nCONSTANT = 1\n__all__ = ['CONSTANT']\n"
                    "copy = globals()['__all__']\n"
                    "same = sys.modules[__name__].__all__\n"
                    "name = getattr(sys.modules[__name__], '__all__')\n"
                )
            }
        )
        self.assertEqual(self.exports(root), [])

    def test_package_must_define_all(self):
        root = self.tree({self.EVENT + "__init__.py": "class Event: pass\n"})
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: no literal __all__"],
        )

    def test_missing_package_is_rejected(self):
        self.assertEqual(
            self.exports(self.tree({})),
            [self.EVENT + "__init__.py: no literal __all__"],
        )

    def test_all_must_name_literal_exports(self):
        for sample in ("__all__ = names\n", "__all__ = [123]\n"):
            with self.subTest(sample=sample):
                root = self.tree({self.EVENT + "__init__.py": sample})
                self.assertEqual(
                    self.exports(root),
                    [self.EVENT + "__init__.py: no literal __all__"],
                )

    def test_direct_functions_and_lambdas_are_rejected(self):
        for definition in (
            "def action(): pass\n",
            "async def action(): pass\n",
            "action = lambda: None\n",
            "action = 'constant'\ndef action(): pass\n",
            "action = ValueError\ndef action(): pass\n",
        ):
            with self.subTest(definition=definition):
                root = self.tree(
                    {self.EVENT + "__init__.py": definition + "__all__ = ['action']\n"}
                )
                self.assertEqual(
                    self.exports(root),
                    [self.EVENT + "__init__.py: __all__ exports function action (FR-44)"],
                )

    def test_import_and_assignment_aliases_cannot_hide_reexported_functions(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "from .bridge import action as public\n"
                    "renamed = public\n__all__ = ['renamed']\n"
                ),
                self.EVENT + "bridge.py": "from .reply import parse as action\n",
                self.EVENT + "reply.py": "def parse(): pass\n",
            }
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function renamed (FR-44)"],
        )

    def test_module_attribute_aliases_cannot_hide_functions(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "import agent_pm.delegate.event.reply as replies\n"
                    "public = replies.parse\n__all__ = ['public']\n"
                ),
                self.EVENT + "reply.py": "def parse(): pass\n",
            }
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function public (FR-44)"],
        )

    def test_exported_class_methods_are_rejected(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "class Event:\n    @classmethod\n    def parse(cls): pass\n"
                    "public = Event.parse\n__all__ = ['public']\n"
                )
            }
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function public (FR-44)"],
        )

    def test_classes_exceptions_constants_and_unexported_functions_are_allowed(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "from .reply import Event, EventError, TAG\n"
                    "def helper(): pass\n__all__: list[str] = ['Event', 'EventError', 'TAG']\n"
                ),
                self.EVENT + "reply.py": (
                    "class Event:\n    def parse(self): pass\n"
                    "class EventError(ValueError): pass\nTAG = 'event'\n"
                ),
            }
        )
        self.assertEqual(self.exports(root), [])

    def test_standard_library_functions_and_uppercase_aliases_are_rejected(self):
        for source in ("json.loads", "os.getcwd", "builtins.len"):
            with self.subTest(source=source):
                module, _, name = source.rpartition(".")
                root = self.tree(
                    {
                        self.EVENT
                        + "__init__.py": f"from {module} import {name} as ACTION\n__all__ = ['ACTION']\n"
                    }
                )
                found = self.exports(root)
                self.assertEqual(len(found), 1)
                self.assertIn("__init__.py:", found[0])
                self.assertIn("ACTION", found[0])

    def test_builtin_aliases_and_bare_builtin_functions_are_rejected(self):
        for sample in ("ACTION = len\n__all__ = ['ACTION']\n", "__all__ = ['len']\n"):
            with self.subTest(sample=sample):
                found = self.exports(self.tree({self.EVENT + "__init__.py": sample}))
                self.assertEqual(len(found), 1)
                self.assertIn("exports function", found[0])

    def test_standard_library_and_builtin_classes_and_constants_are_allowed(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "from pathlib import Path\nfrom builtins import ValueError, Ellipsis\n"
                    "from string import ascii_letters as LETTERS\n"
                    "__all__ = ['Path', 'ValueError', 'Ellipsis', 'LETTERS']\n"
                )
            }
        )
        self.assertEqual(self.exports(root), [])

    def test_constants_shadowing_builtin_names_are_allowed(self):
        root = self.tree({self.EVENT + "__init__.py": "id = 'event'\n__all__ = ['id']\n"})
        self.assertEqual(self.exports(root), [])

    def test_local_alias_keeps_a_class_after_its_original_name_is_redefined(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "class Item: pass\nAlias = Item\ndef Item(): pass\n__all__ = ['Alias']\n"
                )
            }
        )
        self.assertEqual(self.exports(root), [])

    def test_local_alias_keeps_a_function_after_its_original_name_is_reassigned(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "def action(): pass\nAlias = action\naction = 1\n__all__ = ['Alias']\n"
                )
            }
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function Alias (FR-44)"],
        )

    def test_standard_library_source_is_read_without_importing_it(self):
        before = sys.modules.get("antigravity")
        root = self.tree(
            {self.EVENT + "__init__.py": "from antigravity import geohash\n__all__ = ['geohash']\n"}
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function geohash (FR-44)"],
        )
        self.assertIs(sys.modules.get("antigravity"), before)

    def test_unresolved_external_exports_are_rejected(self):
        root = self.tree(
            {
                self.EVENT
                + "__init__.py": "from unknown_package import ACTION\n__all__ = ['ACTION']\n"
            }
        )
        self.assertEqual(
            self.exports(root),
            [self.EVENT + "__init__.py: __all__ exports function ACTION (FR-44)"],
        )

    def test_other_packages_remain_outside_this_ticket(self):
        files = {self.EVENT + "__init__.py": "__all__ = []\n"}
        files.update(
            {
                f"{project}/src/agent_pm/{package}/__init__.py": "def run(): pass\n__all__ = ['run']\n"
                for project, package in (
                    ("utils", "utils"),
                    ("session", "session"),
                    ("delegate", "delegate"),
                    ("delegate", "delegate/composer"),
                    ("manager", "manager"),
                )
            }
        )
        self.assertEqual(self.exports(self.tree(files)), [])


class DelegateObjectExports(Sample):
    PINNED = {
        "agent_pm.delegate": ["read_config", "module_roots"],
        "agent_pm.delegate.composer": [],
    }
    UNRECOGNISED = {
        "PATH": "PATH: str = os.path.join('a', 'b')\n",
        "COUNT": "COUNT: int = int('1')\n",
        "CHOICES": "class Choice(StrEnum):\n    A = 'a'\nCHOICES = tuple(Choice)\n",
        "FROZEN": "FROZEN = frozenset(('a', 'b'))\n",
        "HOME": "HOME = os.path.dirname(os.path.abspath(__file__))\n",
        "ROOTS": "ROOTS = os.pathsep.join(('a', 'b'))\n",
    }

    def exports(self, package, extra="", names=()):
        rel = "delegate/src/" + package.replace(".", "/") + "/__init__.py"
        functions = self.PINNED[package]
        sample = "import os\nfrom enum import StrEnum\n"
        sample += "".join(f"def {name}(): pass\n" for name in functions)
        sample += extra + f"__all__ = {[*functions, *names]!r}\n"
        root = self.tree({rel: sample})
        return [
            item
            for item in exports.object_exports(root)
            if item.startswith(rel + ":") or item.startswith(package + ":")
        ]

    def test_pinned_inventory_can_only_shrink(self):
        expected = {(package, name) for package, names in self.PINNED.items() for name in names}
        actual = {key for key in OBJECT_EXPORTS_ALLOWED if key[0] in self.PINNED}
        self.assertLessEqual(actual, expected)
        for key in actual:
            self.assertTrue(OBJECT_EXPORTS_ALLOWED[key].strip())

    def test_only_pinned_functions_and_objects_are_allowed(self):
        for package in self.PINNED:
            with self.subTest(package=package):
                self.assertEqual(self.exports(package, "class Public: pass\n", ["Public"]), [])
                self.assertEqual(
                    len(self.exports(package, "def new_function(): pass\n", ["new_function"])),
                    1,
                )
                self.assertEqual(
                    len(self.exports(package, "def helper(): pass\nAlias = helper\n", ["Alias"])),
                    1,
                )

    def test_runtime_resource_helpers_are_authorized_only_on_delegate(self):
        self.assertEqual(
            len(
                self.exports(
                    "agent_pm.delegate.composer", "def module_roots(): pass\n", ["module_roots"]
                )
            ),
            1,
        )

    def test_concrete_delegate_cannot_be_exported_under_an_alias(self):
        for name in ("HarnessDelegate", "Renamed"):
            with self.subTest(name=name):
                found = self.exports(
                    "agent_pm.delegate",
                    "class HarnessDelegate: pass\nRenamed = HarnessDelegate\n",
                    [name],
                )
                self.assertEqual(len(found), 1)
                self.assertIn(f"concrete Delegate {name} (FR-47)", found[0])

    def test_missing_all_is_rejected_for_both_packages(self):
        root = self.tree({})
        for package in self.PINNED:
            rel = "delegate/src/" + package.replace(".", "/") + "/__init__.py"
            self.assertIn(f"{rel}: no literal __all__", exports.object_exports(root))

    def test_annotated_values_and_constructor_calls_are_unrecognised_values(self):
        for package in self.PINNED:
            rel = "delegate/src/" + package.replace(".", "/") + "/__init__.py"
            for name, source in self.UNRECOGNISED.items():
                with self.subTest(package=package, name=name):
                    self.assertEqual(
                        self.exports(package, source, [name]),
                        [f"{rel}: __all__ exports unrecognised value {name} (FR-44)"],
                    )

    def test_an_allowlisted_function_that_became_a_constant_is_stale(self):
        stale = "allowlisted but not an exported function or value (FR-44)"
        rel = "delegate/src/agent_pm/delegate/__init__.py"
        sample = "read_config = 'literal'\ndef module_roots(): pass\n"
        sample += "__all__ = ['read_config', 'module_roots']\n"
        found = [
            item
            for item in exports.object_exports(self.tree({rel: sample}))
            if item.startswith((rel + ":", "agent_pm.delegate:"))
        ]
        self.assertEqual(found, [f"agent_pm.delegate: read_config: {stale}"])

    def test_literals_constant_names_and_their_arithmetic_pass_but_typed_callables_fail(self):
        source = (
            "import os\n"
            "NAME = 'x'\n"
            "LABEL: str = 'label'\n"
            "TIMEOUT = 2 * 60 * 60\n"
            "TOTAL = TIMEOUT * 2\n"
            "COPY = NAME\n"
            "SEPARATORS = (os.sep, '/')\n"
        )
        names = ["NAME", "LABEL", "TIMEOUT", "TOTAL", "COPY", "SEPARATORS"]
        for package in self.PINNED:
            rel = "delegate/src/" + package.replace(".", "/") + "/__init__.py"
            with self.subTest(package=package):
                self.assertEqual(self.exports(package, source, names), [])
                for bad in (
                    "BAD: str = lambda: 'value'\n",
                    "def helper(): pass\nBAD: str = helper\n",
                ):
                    self.assertEqual(
                        self.exports(package, bad, ["BAD"]),
                        [f"{rel}: __all__ exports function BAD (FR-44)"],
                    )


class OrchestratorObjectExports(Sample):
    INIT = "linear-orchestrator/src/agent_pm/linear_orchestrator/__init__.py"

    def found(self, source):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/event/__init__.py": "__all__ = []\n",
                "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/run.py": (
                    "class LinearRun: pass\ndef launch(): pass\n"
                ),
                self.INIT: source,
            }
        )
        return [e for e in exports.object_exports(root) if e.startswith(self.INIT)]

    def test_classes_and_constants_pass(self):
        source = (
            "from agent_pm.linear_orchestrator.engine.run import LinearRun\n"
            "KINDS = ('a',)\n__all__ = ['LinearRun', 'KINDS']\n"
        )
        self.assertEqual(self.found(source), [])

    def test_an_exported_function_fails(self):
        for source in (
            "def launch(): pass\n__all__ = ['launch']\n",
            "from agent_pm.linear_orchestrator.engine.run import launch\n__all__ = ['launch']\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found(source), [f"{self.INIT}: __all__ exports function launch (FR-44)"]
                )

    def test_the_package_must_define_all(self):
        self.assertEqual(
            self.found("from agent_pm.linear_orchestrator.engine.run import LinearRun\n"),
            [f"{self.INIT}: no literal __all__"],
        )

    def test_a_missing_package_init_fails(self):
        root = self.tree({"delegate/src/agent_pm/delegate/event/__init__.py": "__all__ = []\n"})
        self.assertIn(f"{self.INIT}: no literal __all__", exports.object_exports(root))


class ObjectExports(Sample):
    SESSION = f"session/{PKG}/session/"
    INIT = SESSION + "__init__.py"
    EXTENSION = SESSION + "extension.py"
    ENTRY_FUNCTIONS = ("read_config",)
    BAD = ("OS", "Shut", "modes", "f", "g", "LAMBDA", "Ghost")
    BAD_SOURCE = (
        "import os as OS\n"
        "from agent_pm.session.engine.config import read_config as Shut\n"
        "from agent_pm.session import modes\n"
        "def f(): pass\n"
        "g = f\n"
        "LAMBDA = lambda: 0\n"
    )

    def init(self, source="", names=()):
        """A session __init__ exporting the allowlisted entry function read_config, then `names`."""
        imported = "from agent_pm.session.engine.config import read_config\n"
        return f"{imported}{source}__all__ = {[*self.ENTRY_FUNCTIONS, *names]!r}\n"

    def found(self, files=None):
        tree = {
            "delegate/src/agent_pm/delegate/event/__init__.py": "__all__ = []\n",
            self.SESSION + "engine/config.py": "def read_config(): pass\n",
            self.SESSION + "modes/__init__.py": "",
            self.INIT: self.init(),
            self.EXTENSION: "__all__ = []\n",
            **(files or {}),
        }
        return [
            entry
            for entry in exports.object_exports(self.tree(tree))
            if entry.startswith(self.SESSION) or entry.startswith("agent_pm.session:")
        ]

    def test_the_real_tree_is_clean(self):
        self.assertEqual(exports.object_exports(REPO), [])

    def test_the_allowlisted_entry_functions_pass_each_with_a_one_line_reason(self):
        self.assertEqual(self.found(), [])
        self.assertEqual(
            {key for key in OBJECT_EXPORTS_ALLOWED if key[0] == "agent_pm.session"},
            {("agent_pm.session", n) for n in self.ENTRY_FUNCTIONS},
        )
        for key, reason in OBJECT_EXPORTS_ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)

    def test_a_function_alias_lambda_module_or_unbound_name_fails_in_either_entry_module(self):
        for rel, text in (
            (self.INIT, self.init(self.BAD_SOURCE, self.BAD)),
            (self.EXTENSION, f"{self.BAD_SOURCE}__all__ = {list(self.BAD)!r}\n"),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.found({rel: text}),
                    sorted(f"{rel}: __all__ exports function {n} (FR-44)" for n in self.BAD),
                )

    def test_a_conditional_alias_or_partial_of_a_function_fails_in_either_entry_module(self):
        source = "import functools\ndef f(): pass\nalias = f if True else f\npart = functools.partial(f)\n"
        names = ("alias", "part")
        for rel, text in (
            (self.INIT, self.init(source, names)),
            (self.EXTENSION, f"{source}__all__ = {list(names)!r}\n"),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.found({rel: text}),
                    sorted(f"{rel}: __all__ exports unrecognised value {n} (FR-44)" for n in names),
                )

    def test_classes_exceptions_enums_constants_and_type_aliases_pass(self):
        extension = (
            "import signal\n"
            "from pathlib import Path\n"
            "from agent_pm.session.engine.types import Error, Event, Mode, Spec\n"
            "from agent_pm.utils import Display\n"
            "Alias = Spec\n"
            "SIGNALS = (signal.SIGHUP, signal.SIGTERM)\n"
            'NAME = "x"\n'
        )
        names = ["Spec", "Mode", "Error", "Event", "Display", "Alias", "SIGNALS", "NAME", "Path"]
        types = (
            "from enum import StrEnum\n"
            "class Spec: pass\n"
            'class Mode(StrEnum):\n    A = "a"\n'
            "class Error(Exception): pass\n"
            "Event = Spec | Mode\n"
        )
        utils = f"utils/{PKG}/utils/"
        self.assertEqual(
            self.found(
                {
                    self.EXTENSION: f"{extension}__all__ = {names!r}\n",
                    self.SESSION + "engine/types.py": types,
                    utils + "__init__.py": "from agent_pm.utils.record import Display\n"
                    'def helper(): pass\n__all__ = ["Display", "helper"]\n',
                    utils + "record.py": "Display = str | None\n",
                }
            ),
            [],
        )

    def test_without_a_literal_all_of_names_an_entry_module_fails(self):
        for source in ("", "__all__ = names()\n", "__all__ = [1]\n", '__all__ = ("A",)\n'):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found({self.EXTENSION: source}), [f"{self.EXTENSION}: no literal __all__"]
                )

    def test_an_allowlisted_name_no_longer_exported_as_a_function_is_stale(self):
        stale = "allowlisted but not an exported function or value (FR-44)"
        init = 'class read_config: pass\n__all__ = ["read_config"]\n'
        self.assertEqual(
            self.found({self.INIT: init}),
            sorted(f"agent_pm.session: {name}: {stale}" for name in self.ENTRY_FUNCTIONS),
        )


class SessionInterfaceSnapshot(Sample):
    def snapshot(self) -> dict:
        with open(os.path.join(REPO, SESSION_INTERFACE), encoding="utf-8") as f:
            return json.load(f)

    def checked(self, snapshot) -> list[str]:
        return exports.session_interface_snapshot(
            self.tree({SESSION_INTERFACE: json.dumps(snapshot)})
        )

    def test_the_repository_matches_its_snapshot(self):
        self.assertEqual(exports.session_interface_snapshot(REPO), [])

    def test_the_snapshot_covers_both_entry_modules_and_their_signatures(self):
        snapshot = self.snapshot()
        self.assertEqual(
            SESSION_INTERFACE_MODULES, ("agent_pm.session", "agent_pm.session.extension")
        )
        self.assertEqual(
            snapshot["agent_pm.session.extension.Host.terminal"],
            "def(self:POSITIONAL_OR_KEYWORD, name:POSITIONAL_OR_KEYWORD, "
            "launch:POSITIONAL_OR_KEYWORD, display:KEYWORD_ONLY=None, events:KEYWORD_ONLY=None, "
            "record:KEYWORD_ONLY=None, metadata:KEYWORD_ONLY=None)",
        )
        self.assertIn("agent_pm.session.Session.show", snapshot)

    def test_a_changed_removed_or_added_export_or_signature_fails(self):
        key = "agent_pm.session.Session.show"
        snapshot = self.snapshot()
        have = snapshot[key]
        for changed, want in (
            (
                {**snapshot, key: "def(self:POSITIONAL_OR_KEYWORD)"},
                f"{SESSION_INTERFACE}: {key}: def(self:POSITIONAL_OR_KEYWORD) -> {have}",
            ),
            (
                {k: v for k, v in snapshot.items() if k != key},
                f"{SESSION_INTERFACE}: {key}: absent -> {have}",
            ),
            (
                {**snapshot, "agent_pm.session.Gone": "def()"},
                f"{SESSION_INTERFACE}: agent_pm.session.Gone: def() -> absent",
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.checked(changed), [want])

    def test_a_missing_snapshot_fails(self):
        self.assertEqual(
            exports.session_interface_snapshot(self.tree({"README.md": ""})),
            [f"{SESSION_INTERFACE}: missing"],
        )


class DelegateInterfaceSnapshot(Sample):
    def snapshot(self) -> dict:
        with open(os.path.join(REPO, DELEGATE_INTERFACE), encoding="utf-8") as f:
            return json.load(f)

    def checked(self, snapshot) -> list[str]:
        return exports.delegate_interface_snapshot(
            self.tree({DELEGATE_INTERFACE: json.dumps(snapshot)})
        )

    def test_the_repository_matches_its_snapshot(self):
        self.assertEqual(exports.delegate_interface_snapshot(REPO), [])

    def test_the_snapshot_covers_the_delegate_package_and_its_signatures(self):
        snapshot = self.snapshot()
        self.assertEqual(DELEGATE_INTERFACE_MODULES, ("agent_pm.delegate",))
        self.assertEqual(
            snapshot["agent_pm.delegate.Delegate.resume"],
            "def(self:POSITIONAL_OR_KEYWORD, mode:KEYWORD_ONLY=None)",
        )
        self.assertEqual(
            snapshot["agent_pm.delegate.Delegate.attach"],
            "classmethod(cls:POSITIONAL_OR_KEYWORD, record_or_workdir:POSITIONAL_OR_KEYWORD, "
            "sid:KEYWORD_ONLY=None)",
        )
        self.assertIn("agent_pm.delegate.DelegateEvent", snapshot)

    def test_a_changed_removed_or_added_export_or_signature_fails(self):
        key = "agent_pm.delegate.Delegate.resume"
        snapshot = self.snapshot()
        have = snapshot[key]
        for changed, want in (
            (
                {**snapshot, key: "def(self:POSITIONAL_OR_KEYWORD)"},
                f"{DELEGATE_INTERFACE}: {key}: def(self:POSITIONAL_OR_KEYWORD) -> {have}",
            ),
            (
                {k: v for k, v in snapshot.items() if k != key},
                f"{DELEGATE_INTERFACE}: {key}: absent -> {have}",
            ),
            (
                {**snapshot, "agent_pm.delegate.run": "def()"},
                f"{DELEGATE_INTERFACE}: agent_pm.delegate.run: def() -> absent",
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.checked(changed), [want])

    def test_a_missing_snapshot_fails(self):
        self.assertEqual(
            exports.delegate_interface_snapshot(self.tree({"README.md": ""})),
            [f"{DELEGATE_INTERFACE}: missing"],
        )
