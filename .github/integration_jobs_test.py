"""Duration scheduling and complete integration-shard coverage."""

import unittest

import integration_jobs as gate


def case(name):
    return type(name, (unittest.TestCase,), {"runTest": lambda _self: None})()


class Assignment(unittest.TestCase):
    def test_longest_tests_are_assigned_first_to_the_least_loaded_shard(self):
        found = [case(name) for name in ("Session", "Delegate", "Manager", "Orchestrator")]
        durations = dict(zip((test.id() for test in found), (10, 8, 2, 1), strict=True))
        split = gate.assign(found, ("1", "2"), durations)
        self.assertEqual([type(test).__name__ for test in split["1"]], ["Session", "Orchestrator"])
        self.assertEqual([type(test).__name__ for test in split["2"]], ["Delegate", "Manager"])
        gate.validate(found, split, ("2", "1"))

    def test_new_tests_use_the_median_and_ties_are_deterministic(self):
        found = [case(name) for name in ("A", "B", "C")]
        known = {found[0].id(): 2.0, found[1].id(): 6.0}
        self.assertEqual(gate.weights(found, known)[found[2].id()], 4.0)
        self.assertEqual(
            gate.assign(found, ("1", "2"), known),
            gate.assign(list(reversed(found)), ("1", "2"), known),
        )
        self.assertEqual(set(gate.weights(found, {}).values()), {1.0})

    def test_invalid_durations_fail(self):
        for duration in (-1, 0, float("inf"), float("nan"), True, "slow"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                gate.weights([case("A")], {"unused": duration})

    def test_missing_duplicate_and_empty_shards_fail(self):
        a, b = case("A"), case("B")
        for suites in (
            {"1": [a], "2": [a, b]},
            {"1": [a], "2": []},
            {"1": [a], "2": [case("C")]},
            {"1": [a, b]},
        ):
            with self.subTest(suites=suites), self.assertRaises(ValueError):
                gate.validate([a, b], suites, ("1", "2"))


class Workflow(unittest.TestCase):
    def test_matrix_must_have_unique_string_labels(self):
        for matrix in ('["1", "2", "1"]', '["1", 2]', "[1, 2]", "[]"):
            text = f"jobs:\n  integration:\n    strategy:\n      matrix:\n        job: {matrix}\n"
            with self.subTest(matrix=matrix):
                if matrix == "[]":
                    self.assertEqual(gate.workflow_jobs(text), [])
                else:
                    with self.assertRaises(ValueError):
                        gate.workflow_jobs(text)
        text = 'jobs:\n  integration:\n    strategy:\n      matrix:\n        job: ["1", "2"]\n'
        self.assertEqual(gate.workflow_jobs(text), ["1", "2"])
