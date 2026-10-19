"""The real JevSelector against a loopback DecisionApi, with a fake key command and proxy env
pointing at a dead loopback port: a request that took the proxy would fail.
"""

import os
import sys
import unittest
from unittest import mock

from agent_pm.selector import (
    Backend,
    Failure,
    JevSelector,
    Question,
    SelectorConfig,
    SelectorError,
    State,
)
from agent_pm.selector.backends.jev import ANTI_INJECTION

from selector.tests.contracts.selector import QUESTION, STATE, SelectorContract
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
            SelectorConfig(Backend.JEV, self.api.url, "jev-1.13.0", timeout, 32000, KEY_COMMAND)
        )

    def failure(self, **kw):
        with self.assertRaises(SelectorError) as caught:
            self.jev(**kw).select(STATE, QUESTION)
        return caught.exception.reason


class JevSelectorContract(SelectorContract, JevCase):
    def make(self):
        return self.jev()


class Wire(JevCase):
    def test_the_mock_api_receives_the_pilot_format(self):
        self.assertEqual(self.jev().select(STATE, QUESTION), "low")
        [request] = self.api.requests
        self.assertEqual((request.method, request.path), ("POST", PATH))
        self.assertEqual(request.headers["Authorization"], "Bearer test-jev-key")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.body["model"], "jev-1.13.0")
        self.assertEqual(set(request.body), {"model", "state", "questions"})

    def test_the_request_carries_the_callers_instructions_and_described_options(self):
        question = Question("Choose the model.", {"a": "desc a", "b": "desc b"})
        self.jev().select(STATE, question)
        [request] = self.api.requests
        self.assertEqual(list(request.body["questions"]), ["choice"])
        choice = request.body["questions"]["choice"]
        self.assertEqual(
            choice,
            {
                "type": "choice",
                "instructions": ANTI_INJECTION + " " + "Choose the model.",
                "criteria": {"a": "desc a", "b": "desc b"},
            },
        )
        self.assertEqual(list(choice["criteria"]), ["a", "b"])

    def test_the_state_is_its_fields_and_the_kept_droppable_items(self):
        self.jev().select(STATE, QUESTION)
        bare = State({"title": "T", "labels": ["Bug"], "description": "D"})
        self.jev().select(bare, QUESTION)
        whole, fields_only = (r.body["state"] for r in self.api.requests)
        self.assertEqual(whole, {**STATE.fields, "comments": list(STATE.droppable[1])})
        self.assertEqual(list(whole), ["title", "description", "comments"])
        self.assertEqual(fields_only, {"title": "T", "labels": ["Bug"], "description": "D"})
        self.assertEqual(list(fields_only), ["title", "labels", "description"])

    def test_scripted_picks_answer_the_first_of_them_in_the_criteria(self):
        self.api.picks = ["b", "high"]
        self.assertEqual(self.jev().select(STATE, QUESTION), "high")
        self.assertEqual(self.jev().select(STATE, Question("Pick.", {"a": "", "b": ""})), "b")
        self.assertEqual(self.jev().select(STATE, Question("Pick.", {"c": "", "d": ""})), "c")

    def test_a_proxy_env_is_bypassed(self):
        self.assertEqual(os.environ["http_proxy"], DEAD_PROXY)
        self.assertEqual(self.jev().select(STATE, QUESTION), "low")
        self.assertEqual(len(self.api.requests), 1)

    def test_a_single_option_sends_nothing(self):
        self.assertEqual(self.jev().select(STATE, Question("Pick.", {"high": "most"})), "high")
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
