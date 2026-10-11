import json
import os
import signal
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    case = os.environ.get("FAKE_CASE", "normal")
    if case in ("ignore-term", "ignore-term-silent"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        keys = ("HOME", "PWD", "CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_VERSION", "CODEX_SANDBOX",
                "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_PERMISSION_PROFILE", "CLAUDECODE", "PYTHONPATH",
                "NO_COLOR", "CODEX_CI", "PAGER", "GIT_PAGER", "GH_PAGER", "FAKE_ADDITION")
        log.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(), "argv": sys.argv[1:],
                              "marker": os.environ.get("CODEX_THREAD_ID"),
                              "env": {key: os.environ.get(key) for key in keys}}) + "\n")
        log.flush()
        if case in ("silent", "ignore-term-silent"):
            time.sleep(30)
            return
        if case == "exit":
            return
        for line in sys.stdin:
            request = json.loads(line)
            log.write(json.dumps(request) + "\n")
            log.flush()
            if request["method"] == "initialize":
                if case == "init-error":
                    emit({"id": request["id"], "error": {"code": -1, "message": "unsupported"}})
                elif case == "init-wrong-type-id":
                    for identifier in (str(request["id"]), float(request["id"]), True):
                        emit({"id": identifier, "result": {"userAgent": "wrong"}})
                else:
                    emit({"method": "notification", "params": {}})
                    emit({"id": request["id"] + 100, "result": {"ignored": True}})
                    emit({"id": request["id"], "result": {"userAgent": "fake"}})
            elif request["method"] == "account/rateLimits/read":
                emit({"method": "account/rateLimits/updated", "params": {}})
                if case == "error":
                    emit({"id": request["id"], "error": {"code": -1, "message": "unavailable"}})
                elif case == "malformed":
                    print("{bad json", flush=True)
                elif case == "oversize":
                    print("x" * (1024 * 1024 + 1), flush=True)
                elif case == "wrong-id":
                    emit({"id": request["id"] + 100, "result": {"rateLimits": {}}})
                elif case == "null-result":
                    emit({"id": request["id"], "result": None})
                elif case == "invalid-utf8":
                    os.write(sys.stdout.fileno(), b"\xff\n")
                elif case == "partial-exit":
                    print('{"id": 2, "result": {', end="", flush=True)
                    return
                elif case == "stderr":
                    print("private native diagnostic", file=sys.stderr, flush=True)
                    emit({"id": request["id"], "error": {"code": -1}})
                elif case == "empty":
                    emit({"id": request["id"], "result": {}})
                else:
                    primary = {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 4000}
                    secondary = {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": 2000}
                    if case == "weekly-full":
                        primary["usedPercent"] = 100
                    if case == "weekly-only":
                        secondary = None
                    if case == "null":
                        primary = secondary = None
                    if case == "invalid-window":
                        primary = {"usedPercent": True, "windowDurationMins": 10080, "resetsAt": "bad"}
                        secondary = {"usedPercent": 60, "windowDurationMins": None, "resetsAt": 2000}
                    if case == "huge-number":
                        primary = {"usedPercent": 10 ** 1000, "windowDurationMins": 10080, "resetsAt": None}
                        secondary = None
                    if case == "five-first":
                        primary, secondary = secondary, primary
                    snapshot = {"primary": primary, "secondary": secondary}
                    result = {"rateLimits": snapshot}
                    if case != "fallback":
                        result["rateLimitsByLimitId"] = {"codex": snapshot}
                        result["rateLimits"] = {"primary": None, "secondary": None}
                    if case == "wrong-type-id":
                        for identifier in (str(request["id"]), float(request["id"]), True):
                            emit({"id": identifier, "result": {"wrong": True}})
                    emit({"id": request["id"], "result": result})
                time.sleep(30)


if __name__ == "__main__":
    main()
