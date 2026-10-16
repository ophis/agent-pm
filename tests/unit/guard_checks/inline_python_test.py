import hashlib

from tests.guard_checks.inline_python import _violations, inline_python
from tests.guard_samples import Sample


class InlinePython(Sample):
    def test_exceptions_match_exact_code_and_stale_entries_fail(self):
        code = "from agent_pm.session.runtimes import tui"
        path = "session/tests/example.py"
        allowed = {
            (
                path,
                hashlib.sha256(code.encode()).hexdigest(),
            ): "Isolates the concrete runtime fixture."
        }
        root = self.tree({path: f"import sys\nargv = [sys.executable, '-c', {code!r}]"})
        self.assertEqual(inline_python(root, allowed=allowed), [])
        other = self.tree(
            {
                path: "import sys\nargv = [sys.executable, '-c', 'from agent_pm.delegate.engine import plan']"
            }
        )
        self.assertEqual(len(inline_python(other, allowed=allowed)), 2)

    def checked(self, source):
        return inline_python(self.tree({"delegate/tests/example.py": source}), allowed={})

    def test_literals_bindings_and_concatenation_are_checked(self):
        for source in (
            "import sys\nargv = [sys.executable, '-c', 'from agent_pm.delegate.engine import plan']",
            "PY = ('python3', '-P')\nCODE = 'from agent_pm.delegate' + '.engine import plan'\nargv = (*PY, '-c', CODE)",
            "def run():\n    code = 'from agent_pm.session.runtimes import tui'\n    argv = ['python3', '-c', code]",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(self.checked(source)), 1)

    def test_standard_library_and_public_packages_are_allowed(self):
        self.assertEqual(
            _violations(
                "import os, sys\nfrom pathlib import Path\nfrom agent_pm.delegate import Delegate\nview = Delegate.attach(sys.argv[1])\nprint(view.content())"
            ),
            [],
        )

    def test_imported_literal_programs_are_checked(self):
        for declaration in ("CODE =", "CODE: str ="):
            with self.subTest(declaration=declaration):
                root = self.tree(
                    {
                        "delegate/tests/program.py": f"{declaration} 'from agent_pm.session.runtimes import tui'",
                        "delegate/tests/entry.py": "import sys\nfrom delegate.tests.program import CODE\nargv = [sys.executable, '-c', CODE]",
                    }
                )
                self.assertEqual(len(inline_python(root, allowed={})), 1)

    def test_syntax_nonpublic_imports_and_private_object_access_are_rejected(self):
        for code in (
            "def broken(",
            "from delegate.tests.harness import HarnessCase",
            "from agent_pm.session.engine import types",
            "from agent_pm.delegate import _private",
            "import agent_pm.delegate as d\nd._private()",
            "from agent_pm.delegate import Delegate as D\nview = D.attach('w')\nview._read()",
            "from agent_pm.delegate import Delegate\nv: Delegate = Delegate.attach('w')\nv._read()",
            "from agent_pm.delegate import Delegate\nv = Delegate.attach('w')\ngetattr(v, '_read')()",
            "import importlib\nimportlib.import_module('agent_pm.session.runtimes')",
        ):
            with self.subTest(code=code):
                self.assertTrue(_violations(code))

    def test_git_config_dynamic_code_and_non_agent_private_attributes_are_outside_the_rule(self):
        self.assertEqual(
            self.checked(
                "argv = ['git', '-c', 'user.name=Example']\nargv = ['python3', '-c', f'print({value})']"
            ),
            [],
        )
        self.assertEqual(
            _violations("class Example: pass\nvalue = Example()\nvalue._private = 1"), []
        )
