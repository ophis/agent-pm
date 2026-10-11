"""Shared pure tools, one module per purpose: config_file (the core config's files), paths (path checks), files (safe
reads). A helper more than one layer (or repo.py) needs lives here, never copied. Stdlib only: imports no layer and no
other src/ module but utils, so any of them may import it (tests/guards.py utils_imports)."""
