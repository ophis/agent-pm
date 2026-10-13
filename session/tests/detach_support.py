"""Shared test fixtures and support."""

CHILD = """import json, os, signal, sys, time
report, go = sys.argv[1:]
with open(report + ".tmp", "w") as f: json.dump(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])), f)
os.replace(report + ".tmp", report)
signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGHUP, signal.SIGTERM, signal.SIGINT])
for _ in range(600):
    if os.path.exists(go): break
    time.sleep(0.05)
"""
