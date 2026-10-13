"""Install the subprocess integration transport from test-only PYTHONPATH."""

import os

from fake_transport import ENV, install

if path := os.environ.get(ENV):
    transport = install(path)
