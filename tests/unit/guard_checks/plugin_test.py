import json

from tests.case import REPO
from tests.guard_checks import plugin
from tests.guard_samples import Sample

CLAUDE, CODEX = ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"


class PluginManifests(Sample):
    SKILLS = ["./delegate/skills/", "./manager/skills/"]

    def manifest(self, **fields):
        return json.dumps({"name": "agent-pm", "version": "1.0.0", "skills": self.SKILLS, **fields})

    def shipped(self, files=()):
        files = {
            CLAUDE: self.manifest(),
            CODEX: self.manifest(),
            "delegate/skills/a/SKILL.md": "",
            "manager/skills/m/SKILL.md": "",
            "manager/skills/t/scripts/ctmux": "",
            **dict(files),
        }
        return self.tree({rel: text for rel, text in files.items() if text is not None})

    def test_the_real_manifests_ship_every_skill(self):
        self.assertEqual(plugin.plugin_manifests(REPO), [])

    def test_manifests_listing_every_project_skill_pass(self):
        self.assertEqual(plugin.plugin_manifests(self.shipped()), [])

    def test_a_single_skills_path_string_is_accepted(self):
        root = self.shipped(
            {
                CLAUDE: self.manifest(skills="./skills/"),
                CODEX: self.manifest(skills="./skills/"),
                "skills/a/SKILL.md": "",
                "delegate/skills/a/SKILL.md": None,
                "manager/skills/m/SKILL.md": None,
            }
        )
        self.assertEqual(plugin.plugin_manifests(root), [])

    def test_a_missing_manifest(self):
        root = self.shipped({CODEX: None})
        self.assertEqual(plugin.plugin_manifests(root), [f"{CODEX}: missing"])

    def test_a_manifest_missing_a_project_skill(self):
        root = self.shipped({CODEX: self.manifest(skills=["./delegate/skills/"])})
        self.assertEqual(
            plugin.plugin_manifests(root),
            [f"{CODEX}: skills miss manager/skills/m/SKILL.md"],
        )

    def test_a_skill_outside_the_projects(self):
        root = self.shipped(
            {CODEX: self.manifest(skills=[*self.SKILLS, "./extra/"]), "extra/x/SKILL.md": ""}
        )
        self.assertEqual(
            plugin.plugin_manifests(root),
            [f"{CODEX}: skills ship extra/x/SKILL.md, not a plugin skill"],
        )

    def test_a_skills_path_not_relative_to_the_plugin_root(self):
        root = self.shipped(
            {CODEX: self.manifest(skills=["delegate/skills/", "./manager/skills/"])}
        )
        self.assertEqual(
            plugin.plugin_manifests(root),
            [
                f'{CODEX}: skills path "delegate/skills/" does not start with ./',
                f"{CODEX}: skills miss delegate/skills/a/SKILL.md",
            ],
        )

    def test_a_name_or_version_other_than_the_claude_code_manifest(self):
        root = self.shipped({CODEX: self.manifest(name="other", version="0.9.0")})
        self.assertEqual(
            plugin.plugin_manifests(root),
            [
                f'{CODEX}: name "other", not {CLAUDE}\'s "agent-pm"',
                f'{CODEX}: version "0.9.0", not {CLAUDE}\'s "1.0.0"',
            ],
        )


class PluginSkills(Sample):
    def test_the_real_plugin_ships_exactly_its_three_skills(self):
        self.assertEqual(plugin.plugin_skills(REPO), [])

    def test_a_missing_extra_or_repeated_skill(self):
        root = self.tree(
            {
                "delegate/skills/act-as/SKILL.md": "",
                "manager/skills/manage/SKILL.md": "",
                "skills/manage/SKILL.md": "",
                "manager/skills/extra/SKILL.md": "",
            }
        )
        self.assertEqual(
            plugin.plugin_skills(root),
            [
                "skill manage: shipped 2 times",
                "skill tmux: missing",
                "skill extra: not one of act-as, manage, tmux",
            ],
        )
