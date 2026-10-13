from agent_pm.session import (
    Access,
    DriverError,
    NotDisplayable,
    Spec,
)

from session.tests.case import Case


class EnvironmentSources(Case):
    def test_spec_accepts_both_environment_sources(self):
        spec = Spec(workdir="/w", access=Access(env={"A": "1"}), env={"B": "2"})
        self.assertEqual((spec.access.env, dict(spec.env)), ({"A": "1"}, {"B": "2"}))


class DisplayError(Case):
    def test_not_displayable_is_a_driver_error(self):
        self.assertTrue(issubclass(NotDisplayable, DriverError))
        self.assertEqual(
            str(NotDisplayable("host fake cannot display")), "host fake cannot display"
        )
