"""Installer JSONC edits, without launching tools or touching credentials."""

import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.case import REPO, Case


def helper(script="install.sh"):
    text = Path(REPO, "scripts", script).read_text()
    body = text.split("opencode_config_edit() {\n", 1)[1].split("\n}\n", 1)[0]
    return body.split("python3 -I -c '\n", 1)[1].split("' ", 1)[0]


class OpenCodeConfigTest(Case):
    def setUp(self):
        self.conf = Path(self.home, "config")
        self.conf.mkdir()
        self.state = Path(self.home, "state")
        self.state.mkdir()
        self.path = self.conf / "opencode.jsonc"
        self.before = self.state / "opencode.jsonc.orig"
        self.installed = self.state / "opencode.jsonc.installed"

    def edit(self, *args):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", *map(str, args)]), redirect_stdout(output):
            exec(helper(), {})
        return output.getvalue().strip()

    def plan(self):
        return self.edit("plan", self.conf, self.state, "lan", "http://localhost/v1", "m", "65536")

    def add(self):
        target = self.plan()
        self.assertEqual(target, str(self.path))
        shutil.copyfile(self.state / "opencode-provider.plan", target)
        return json.loads(self.path.read_text())["provider"]["lan"]

    def remove(self):
        self.edit("remove-provider", self.path, self.state, "lan")

    def test_new_provider_and_budget(self):
        value = self.add()
        self.assertEqual(value["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(value["options"], {"baseURL": "http://localhost/v1"})
        self.assertEqual(value["models"]["m"]["name"], "m")
        self.assertEqual(
            value["models"]["m"]["limit"], {"context": 65536, "input": 32768, "output": 16384}
        )
        before = self.path.read_bytes()
        self.assertEqual(self.plan(), "")
        self.assertEqual(self.path.read_bytes(), before)

    def test_jsonc_comments_strings_trailing_commas_and_other_provider_survive(self):
        original = '{\n// keep\n"theme": "a,} // \\"", "provider": {"mine": {},},\n}\n'
        self.path.write_text(original)
        self.plan()
        shutil.copyfile(self.state / "opencode-provider.plan", self.path)
        after = self.path.read_text()
        self.assertIn('// keep\n"theme": "a,} // \\""', after)
        self.assertIn('"mine": {}', after)
        self.remove()
        self.assertIn('"mine": {}', self.path.read_text())
        self.assertNotIn('"lan"', self.path.read_text())

    def test_existing_provider_in_either_file_is_untouched(self):
        for filename in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=filename):
                path = self.conf / filename
                path.write_text('{"provider": {"lan": {"name": "mine"}}}')
                self.assertEqual(self.plan(), "")
                self.assertEqual(path.read_text(), '{"provider": {"lan": {"name": "mine"}}}')
                self.assertFalse((self.state / "opencode-provider-lan.json").exists())
                path.unlink()

    def test_invalid_config_fails_without_writing_or_echoing_values(self):
        for text in (
            '{"private": "sensitive",',
            "[]",
            '{"provider": []}',
            '{"provider": {}, "provider": {}}',
            '{"enabled_providers": ["other"]}',
            '{"disabled_providers": ["lan"]}',
        ):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(SystemExit) as caught:
                    self.plan()
                self.assertNotIn("sensitive", str(caught.exception))
                self.assertEqual(self.path.read_text(), text)
                self.assertFalse((self.state / "opencode-provider.plan").exists())

    def test_cleanup_preserves_other_settings_and_user_modified_provider(self):
        value = self.add()
        self.path.write_text(json.dumps({"provider": {"lan": value, "mine": {}}, "theme": "mine"}))
        self.remove()
        self.assertEqual(
            json.loads(self.path.read_text()), {"provider": {"mine": {}}, "theme": "mine"}
        )
        self.path.unlink()
        value = self.add()
        value["options"]["baseURL"] = "http://mine/v1"
        self.path.write_text(json.dumps({"provider": {"lan": value}}))
        before = self.path.read_bytes()
        self.remove()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cleanup_removes_only_new_container(self):
        self.add()
        self.remove()
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.path.write_text('{"provider": {}}')
        self.plan()
        shutil.copyfile(self.state / "opencode-provider.plan", self.path)
        self.remove()
        self.assertEqual(json.loads(self.path.read_text()), {"provider": {}})

    def test_plugin_cleanup_preserves_user_edits_and_other_plugins(self):
        self.before.write_text('{// original\n"theme":"mine", "plugin":["old"],}\n')
        self.installed.write_text('{"theme":"mine","plugin":["old","installed"]}')
        self.path.write_text('{// later\n"theme":"new", "plugin":["old","installed","user"]}')
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertIn("// later", self.path.read_text())
        self.assertIn('"theme":"new"', self.path.read_text())
        self.assertIn('"user"', self.path.read_text())
        self.assertNotIn('"installed"', self.path.read_text())

    def test_plugin_cleanup_restores_original_bytes_or_removes_new_empty_file(self):
        original = '{// keep exact\n "theme":"mine",}\n'
        self.before.write_text(original)
        self.installed.write_text('{"theme":"mine","plugin":["installed"]}')
        self.path.write_text(self.installed.read_text())
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertEqual(self.path.read_text(), original)
        self.before.write_text("")
        self.installed.write_text('{"plugin":["installed"]}')
        self.path.write_text(self.installed.read_text())
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertFalse(self.path.exists())

    def test_generated_schema_is_removed_but_later_settings_remain(self):
        self.before.write_text("")
        installed = {"$schema": "https://opencode.ai/config.json", "plugin": ["installed"]}
        self.installed.write_text(json.dumps(installed))
        self.path.write_text(json.dumps({**installed, "theme": "mine"}))
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertEqual(json.loads(self.path.read_text()), {"theme": "mine"})

    def test_plugin_rerecord_does_not_claim_later_user_plugin(self):
        self.before.write_text("")
        self.path.write_text('{"plugin":["earlier-user","agent-pm@old"]}')
        self.edit("record-plugin", self.path, self.before, self.installed, "agent-pm@old")
        self.path.write_text('{"plugin":["earlier-user","user","agent-pm@new"]}')
        self.edit("record-plugin", self.path, self.before, self.installed, "agent-pm@new")
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertEqual(json.loads(self.path.read_text()), {"plugin": ["earlier-user", "user"]})

    def test_plugin_cleanup_restores_original_replaced_spec(self):
        original = '{"plugin":["agent-pm@original"]}'
        self.before.write_text(original)
        self.installed.write_text('{"plugin":["agent-pm@installed"]}')
        self.path.write_text(self.installed.read_text())
        self.edit("remove-plugin", self.path, self.before, self.installed)
        self.assertEqual(self.path.read_text(), original)

    def test_failed_atomic_cleanup_keeps_original_and_no_temp_files(self):
        self.add()
        before = self.path.read_bytes()
        with (
            mock.patch("os.replace", side_effect=OSError("private detail")),
            self.assertRaises(SystemExit) as caught,
        ):
            self.remove()
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.conf.iterdir()), [self.path])

    def test_installer_and_cleanup_share_editor(self):
        self.assertEqual(helper(), helper("install-cleanup.sh"))
