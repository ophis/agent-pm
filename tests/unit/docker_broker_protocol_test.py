import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.case import REPO, Case

PROTOCOL = Path(REPO, "protocol/docker-broker")


class DockerBrokerProtocol(Case):
    def test_every_shared_fixture_has_its_expected_schema_outcome(self):
        schema = json.loads((PROTOCOL / "protocol.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        index = json.loads((PROTOCOL / "fixtures/index.json").read_text())
        self.assertTrue(index["cases"])
        self.assertEqual(index["protocol_version"], "8")
        names = [case["file"] for case in index["cases"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            {p.name for p in (PROTOCOL / "fixtures").glob("*.json")} - {"index.json"},
        )
        covered = set()
        for case in index["cases"]:
            with self.subTest(fixture=case["file"]):
                self.assertEqual(Path(case["file"]).name, case["file"])
                definition = case["definition"]
                self.assertIn(definition, schema["$defs"])
                validator = Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})
                data = json.loads((PROTOCOL / "fixtures" / case["file"]).read_text())
                errors = list(validator.iter_errors(data))
                self.assertEqual(not errors, case["valid"], [e.message for e in errors])
                covered.add((definition, case["valid"]))
        for definition in (
            "manifest",
            "version_input",
            "version_output",
            "build_input",
            "build_output",
            "start_input",
            "start_output",
            "refresh_input",
            "refresh_output",
            "close_input",
            "close_output",
            "status_input",
            "status_output",
            "workspace_rm_input",
            "workspace_rm_output",
            "error",
        ):
            for valid in (True, False):
                self.assertIn((definition, valid), covered)

    def test_manifest_required_fields_and_rejected_unknown_fields_are_exercised(self):
        index = json.loads((PROTOCOL / "fixtures/index.json").read_text())
        cases = {case["file"]: case for case in index["cases"]}
        for field in (
            "name",
            "generation",
            "image",
            "cwd",
            "workspace",
            "claude_dir",
            "github",
            "labels",
        ):
            self.assertFalse(cases[f"manifest-missing-{field}.json"]["valid"])
        self.assertFalse(cases["manifest-unknown-field.json"]["valid"])
        self.assertTrue(cases["manifest-no-hook.json"]["valid"])
