"""Child script for runner supervision assertions."""

from pathlib import Path

HERE = Path(__file__).resolve().parent

# A real os.abort() makes macOS file a crash report per run: print faulthandler's report and exit
# 134 instead.
SCRIPT = """import faulthandler, os, sys, unittest
sys.path.insert(0, {github!r})
import run_tests

def abort():
    sys.stderr.write("Fatal Python error: Aborted\\n\\n")
    sys.stderr.flush()
    faulthandler.dump_traceback()
    os._exit(134)

class T(unittest.TestCase):
    def test_a(self):
        if {mode!r} == "mid":
            abort()
        self.assertNotEqual({mode!r}, "fail")

code = run_tests.supervise("Unit x")
if code is None:
    if {mode!r} == "early":
        sys.exit(2)
    code = run_tests.run(unittest.TestLoader().loadTestsFromTestCase(T), "Unit x")
    if {mode!r} == "after":
        abort()
sys.exit(code)
"""
