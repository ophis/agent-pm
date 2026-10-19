"""The real JevSelector against a loopback DecisionApi, with a fake key command and proxy env
pointing at a dead loopback port: a request that took the proxy would fail.
"""

import os
import sys
import unittest
from unittest import mock

from agent_pm.selector import Backend, Failure, JevSelector, SelectorConfig, SelectorError

from selector.tests.contracts.selector import CHOICES, STATE, SelectorContract
from selector.tests.fakes.decision_api import ELSEWHERE, PATH, DecisionApi, Reply
from utils.tests.case import Case

FAKES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fakes")
KEY_COMMAND = (sys.executable, os.path.join(FAKES, "key.py"))
DEAD_PROXY = "http://127.0.0.1:9"


class JevCase(Case):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            env[name] = DEAD_PROXY
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.api = self.enterContext(DecisionApi())

    def jev(self, timeout=5.0):
        return JevSelector(
            SelectorConfig(Backend.JEV, self.api.url, "jev-1.13.0", timeout, KEY_COMMAND)
        )

    def failure(self, **kw):
        with self.assertRaises(SelectorError) as caught:
            self.jev(**kw).select(STATE, CHOICES)
        return caught.exception.reason


class JevSelectorContract(SelectorContract, JevCase):
    def make(self):
        return self.jev()


class Wire(JevCase):
    def test_the_mock_api_receives_the_pilot_format(self):
        self.assertEqual(self.jev().select(STATE, CHOICES), CHOICES[0])
        [request] = self.api.requests
        self.assertEqual((request.method, request.path), ("POST", PATH))
        self.assertEqual(request.headers["Authorization"], "Bearer test-jev-key")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.body["model"], "jev-1.13.0")
        self.assertEqual(request.body["state"], {"text": STATE})
        question = request.body["questions"]["choice"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(list(question["criteria"]), list(CHOICES))

    def test_a_proxy_env_is_bypassed(self):
        self.assertEqual(os.environ["http_proxy"], DEAD_PROXY)
        self.assertEqual(self.jev().select(STATE, CHOICES), CHOICES[0])
        self.assertEqual(len(self.api.requests), 1)

    def test_a_single_choice_sends_nothing(self):
        self.assertEqual(self.jev().select(STATE, ("high",)), "high")
        self.assertEqual(self.api.requests, [])

    def test_a_redirect_is_not_followed(self):
        self.api.reply = Reply(302, b"", {"Location": ELSEWHERE})
        self.assertEqual(self.failure(), Failure.ERROR)
        self.assertEqual(self.api.elsewhere, 0)
        self.assertEqual([r.path for r in self.api.requests], [PATH])

    def test_a_slow_reply_times_out(self):
        self.api.reply = Reply(delay=5.0)
        self.assertEqual(self.failure(timeout=0.2), Failure.TIMEOUT)

    def test_an_answer_outside_the_choices_is_invalid(self):
        self.api.reply = Reply(body=b'{"answers": {"choice": {"choice": "other"}}}')
        self.assertEqual(self.failure(), Failure.INVALID)


if __name__ == "__main__":
    unittest.main()
