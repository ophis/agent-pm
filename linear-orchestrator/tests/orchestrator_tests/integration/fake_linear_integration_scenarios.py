import functools
import os
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.case import Case
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.fake_linear_integration_data import (
    ENGINEER,
    ENGINEER_EMAIL,
    HARNESS,
    HARNESS_EMAIL,
    PROJECT,
)
from orchestrator_tests.integration.transport.fake_transport import install


class ServedScenario(Case):

    def setUp(self):
        self.fake = fake = fake_linear.FakeLinear(harness=HARNESS)
        self.harness = fake.user(HARNESS_EMAIL, 'Harness', HARNESS)
        self.engineer = fake.user(ENGINEER_EMAIL, 'Engineer', ENGINEER)
        self.human = fake.user('Me@X.com', 'Me')
        fake.issue('Session registry', identifier='TASK-7', state='in_progress', assignee=self.engineer, project=PROJECT)
        self.url = fake_linear.serve(self, fake)
        env = mock.patch.dict(os.environ, {'FAKE_LINEAR_TRANSPORT': fake_linear.seam(self, fake, self.url)})
        env.start()
        self.addCleanup(env.stop)
        transport = install(os.environ['FAKE_LINEAR_TRANSPORT'])
        self.addCleanup(transport.close)
        self.gql = functools.partial(linear.linear_gql, service=HARNESS)
        self.eng = functools.partial(linear.linear_gql, service=ENGINEER)

    def log(self):
        return [(r.op, r.account, r.result) for r in self.fake.requests]
