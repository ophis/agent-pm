"""selector/backends/jev.py: JevSelector's config, key, exchange and failure codes."""

import contextlib
import http.client
import io
import json
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from agent_pm.selector import (
    JEV_DEFAULT,
    Backend,
    Failure,
    JevSelector,
    SelectorConfig,
    SelectorError,
)
from agent_pm.selector.backends.jev import INSTRUCTIONS, MAX_REPLY

from utils.tests.case import Case
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired

KEY = "test-jev-key"
URL = "https://api.example/v1/systemone"
STATE = "state-marker"
CHOICES = ("low-marker", "high-marker")
CONFIG = SelectorConfig(Backend.JEV, URL, "jev-x", 3.0, ("/bin/key", "item"))


def reply(choice="high-marker"):
    return json.dumps({"answers": {"choice": {"type": "choice", "choice": choice}}}).encode()


def respond(body):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def http_error(code):
    return urllib.error.HTTPError(URL, code, "Error", {}, io.BytesIO(b"reply-marker"))


class Opener:
    def __init__(self, body=None, error=None):
        self.body = reply() if body is None else body
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return respond(self.body)


class JevCase(Case):
    def ask(self, *, body=None, error=None, run=None, config=CONFIG, state=STATE, choices=CHOICES):
        self.opener = Opener(body, error)
        self.run = run or mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout=b"%s\n" % KEY.encode())
        )
        self.build = mock.Mock(return_value=self.opener)
        with (
            mock.patch("subprocess.run", self.run),
            mock.patch.object(urllib.request, "build_opener", self.build),
            mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("urlopen")),
        ):
            return JevSelector(config).select(state, choices)

    def failure(self, **kw):
        with self.assertRaises(SelectorError) as caught:
            self.ask(**kw)
        return caught.exception


class Config(JevCase):
    def test_the_defaults(self):
        self.assertEqual(
            JEV_DEFAULT,
            SelectorConfig(
                Backend.JEV,
                "https://api.typesafe.ai/v1/systemone",
                "jev-1.13.0",
                2.0,
                ("/usr/bin/security", "find-generic-password", "-s", "typesafe-jev", "-w"),
            ),
        )
        self.assertIs(JevSelector().config, JEV_DEFAULT)

    def test_only_https_or_loopback_http(self):
        for url in (
            "https://api.typesafe.ai/v1/systemone",
            "http://127.0.0.1:8000/v1/systemone",
            "http://127.9.9.9/v1/systemone",
            "http://[::1]:8000/v1/systemone",
        ):
            with self.subTest(url=url):
                JevSelector(replace(CONFIG, url=url))
        for url in (
            "http://example.com/v1/systemone",
            "http://localhost:8000/v1/systemone",
            "http://10.0.0.1/v1/systemone",
            "ftp://127.0.0.1/v1/systemone",
            "https:///v1/systemone",
            "api.typesafe.ai/v1/systemone",
            "https://u@api.typesafe.ai/v1/systemone",
            "http://u:p@127.0.0.1:8000/v1/systemone",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                JevSelector(replace(CONFIG, url=url))

    def test_a_kev_config_is_refused(self):
        with self.assertRaises(ValueError):
            JevSelector(replace(CONFIG, backend=Backend.KEV))


class Choices(JevCase):
    def test_bad_choices_are_refused_before_the_key(self):
        for choices in ((), ("low", " "), ("low", "low")):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                self.ask(choices=choices)
            self.run.assert_not_called()
            self.build.assert_not_called()

    def test_a_single_choice_reads_no_key_and_sends_nothing(self):
        self.assertEqual(self.ask(choices=["only"]), "only")
        self.run.assert_not_called()
        self.build.assert_not_called()


class Key(JevCase):
    def test_the_key_command_runs_as_argv_without_a_shell(self):
        self.ask()
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        self.assertEqual(list(args[0]), ["/bin/key", "item"])
        self.assertEqual(
            kwargs,
            {
                "capture_output": True,
                "stdin": DEVNULL,
                "timeout": 3.0,
                "check": False,
            },
        )
        request, _ = self.opener.calls[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {KEY}")

    def test_no_key_cases(self):
        def ran(code=0, out=b"key\n"):
            return mock.Mock(return_value=SimpleNamespace(returncode=code, stdout=out))

        cases = {
            "nonzero exit": ran(44),
            "not utf-8": ran(out=b"\xff"),
            "empty": ran(out=b"\n"),
            "whitespace": ran(out=b"a key\n"),
            "not printable": ran(out=b"a\x07key"),
            "missing command": mock.Mock(side_effect=FileNotFoundError("/bin/key")),
            "not executable": mock.Mock(side_effect=PermissionError("/bin/key")),
            "bad argv": mock.Mock(side_effect=ValueError("embedded null byte")),
        }
        for name, run in cases.items():
            with self.subTest(name):
                self.assertEqual(self.failure(run=run).reason, Failure.NO_KEY)
                self.assertEqual(self.opener.calls, [])
        with self.subTest("no key command"):
            self.assertEqual(
                self.failure(config=replace(CONFIG, key_command=())).reason, Failure.NO_KEY
            )
            self.run.assert_not_called()

    def test_the_key_is_not_kept(self):
        selector = JevSelector(CONFIG)
        with (
            mock.patch(
                "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=KEY.encode())
            ),
            mock.patch.object(urllib.request, "build_opener", return_value=Opener()),
        ):
            selector.select(STATE, CHOICES)
        self.assertEqual(vars(selector), {"config": CONFIG})


class Exchange(JevCase):
    def test_one_post_in_the_pilot_format(self):
        self.assertEqual(self.ask(), "high-marker")
        self.assertEqual(len(self.opener.calls), 1)
        request, timeout = self.opener.calls[0]
        self.assertEqual((request.full_url, request.get_method(), timeout), (URL, "POST", 3.0))
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(
            body,
            {
                "model": "jev-x",
                "state": {"text": STATE},
                "questions": {
                    "choice": {
                        "type": "choice",
                        "instructions": INSTRUCTIONS,
                        "criteria": {"low-marker": "low-marker", "high-marker": "high-marker"},
                    }
                },
            },
        )
        self.assertEqual(list(body["questions"]["choice"]["criteria"]), list(CHOICES))

    def test_the_opener_has_no_proxy(self):
        self.ask()
        handlers = self.build.call_args.args
        proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual([p.proxies for p in proxies], [{}])

    def test_a_redirect_is_an_error(self):
        self.ask()
        redirect = [
            h
            for h in self.build.call_args.args
            if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect), 1)
        headers = http.client.HTTPMessage()
        self.assertIsNone(redirect[0].redirect_request(None, None, 302, "Found", headers, URL))
        for code in (301, 302, 307, 308):
            with self.subTest(code=code):
                self.assertEqual(self.failure(error=http_error(code)).reason, Failure.ERROR)

    def test_failed_exchanges_are_errors(self):
        cases = {
            "status 401": {"error": http_error(401)},
            "status 500": {"error": http_error(500)},
            "refused": {"error": urllib.error.URLError(ConnectionRefusedError())},
            "reset": {"error": ConnectionResetError()},
            "bad status line": {"error": http.client.BadStatusLine("x")},
            "oversized": {"body": b" " * MAX_REPLY + reply()},
            "not json": {"body": b"reply-marker"},
            "not utf-8": {"body": b'{"answers": "\xff"}'},
            "too deep": {"body": b"[" * 100000 + b"]" * 100000},
            "not an object": {"body": b'["high-marker"]'},
            "no answers": {"body": b"{}"},
            "answers not an object": {"body": b'{"answers": []}'},
            "no choice question": {"body": b'{"answers": {}}'},
            "choice not an object": {"body": b'{"answers": {"choice": "high-marker"}}'},
            "no choice key": {"body": b'{"answers": {"choice": {"type": "choice"}}}'},
            "empty": {"body": b""},
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assertEqual(self.failure(**kw).reason, Failure.ERROR)

    def test_a_reply_at_the_limit_is_read(self):
        body = reply()
        self.assertEqual(self.ask(body=body + b" " * (MAX_REPLY - len(body))), "high-marker")


class Timeout(JevCase):
    def test_key_and_request_timeouts(self):
        slow_key = mock.Mock(side_effect=TimeoutExpired(["/bin/key"], 3.0, output=b"KEY"))
        self.assertEqual(self.failure(run=slow_key).reason, Failure.TIMEOUT)
        self.assertEqual(self.opener.calls, [])
        for error in (TimeoutError(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=error):
                self.assertEqual(self.failure(error=error).reason, Failure.TIMEOUT)
                self.assertEqual(self.opener.calls[0][1], 3.0)


class Answer(JevCase):
    def test_an_answer_outside_the_choices_is_invalid(self):
        for choice in ("other", "HIGH-MARKER", "", None, 1, ["high-marker"]):
            with self.subTest(choice=choice):
                self.assertEqual(self.failure(body=reply(choice)).reason, Failure.INVALID)


class Errors(JevCase):
    CASES = {
        "no key": {"run": mock.Mock(side_effect=FileNotFoundError(KEY))},
        "undecodable key": {
            "run": mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout=b"%s\xff" % KEY.encode())
            )
        },
        "key timeout": {
            "run": mock.Mock(side_effect=TimeoutExpired(["k"], 3.0, output=KEY.encode()))
        },
        "redirect": {"error": http_error(302)},
        "status": {"error": http_error(500)},
        "timeout": {"error": TimeoutError(URL)},
        "not json": {"body": b"reply-marker " + STATE.encode()},
        "invalid": {"body": reply("reply-marker")},
    }

    def test_every_error_carries_only_its_code(self):
        markers = (KEY, STATE, *CHOICES, URL, "reply-marker")
        for name, kw in self.CASES.items():
            with self.subTest(name):
                error = self.failure(**kw)
                self.assertEqual(str(error), error.reason.value)
                self.assertEqual(error.args, (error.reason.value,))
                text = f"{error!r} {error.args!r}"
                self.assertFalse([m for m in markers if m in text])
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)

    def test_only_selector_or_value_errors_leave_select(self):
        self.assertEqual(self.ask(state="\ud800 lone surrogate"), "high-marker")
        self.assertEqual(
            json.loads(self.opener.calls[0][0].data)["state"]["text"], "\ud800 lone surrogate"
        )
        for name, kw in self.CASES.items():
            with self.subTest(name):
                self.failure(**kw)

    def test_nothing_is_logged(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertNoLogs():
            self.ask()
            for kw in self.CASES.values():
                self.failure(**kw)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


if __name__ == "__main__":
    unittest.main()
