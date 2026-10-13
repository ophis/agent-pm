from agent_pm.linear_orchestrator.engine import target
from orchestrator_test_helpers.unit.target.slug_fixtures import Slug as SlugBase


class Slug1(SlugBase):
    def test_rules(self):
        self.assertEqual(target.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(target.slug("Engineering 骨架"), "engineering")
        self.assertEqual(target.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(target.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(target.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))
