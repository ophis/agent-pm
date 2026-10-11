"""Puts the code under test on sys.path, core/src ahead of orchestrator/src as orchestrator/src/config.py has it, then
core/skills/tmux/scripts; ROOT is the repo root."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = [os.path.join(ROOT, d) for d in ("core/src", "orchestrator/src", "core/skills/tmux/scripts")]
sys.path[:0] = [p for p in _SRC if p not in sys.path]
