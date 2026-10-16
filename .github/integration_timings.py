"""Validate complete Ubuntu attempts and explicitly regenerate median test timings."""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import integration_jobs


def aggregate(samples, expected):
    groups = defaultdict(list)
    for manifest, rows in samples:
        groups[manifest["run_id"], manifest["attempt"]].append((manifest, rows))
    if not groups:
        raise ValueError("no timing samples")
    values, sources = defaultdict(list), []
    for key, group in sorted(groups.items()):
        first = group[0][0]
        labels = first["shards"]
        if not labels or len(labels) != len(set(labels)):
            raise ValueError(f"{key}: invalid shard labels")
        if Counter(row[0]["shard"] for row in group) != Counter(labels):
            raise ValueError(f"{key}: missing or duplicate shard samples")
        seen = []
        for manifest, rows in group:
            fields = ("sha", "head_sha", "runner", "python", "tmux", "shards", "discovered")
            if any(manifest[field] != first[field] for field in fields):
                raise ValueError(f"{key}: inconsistent shard metadata")
            if manifest["runner"] != "ubuntu-24.04" or manifest["exit_code"] != 0:
                raise ValueError(f"{key}: requires successful ubuntu-24.04 samples")
            if Counter(manifest["discovered"]) != Counter(expected):
                raise ValueError(f"{key}: discovered tests differ from this checkout")
            ids = [row["test"] for row in rows]
            if Counter(ids) != Counter(manifest["tests"]) or len(ids) != len(set(ids)):
                raise ValueError(f"{key}: missing or duplicate test measurements")
            durations = {row["test"]: row["seconds"] for row in rows}
            integration_jobs.weights([], durations)
            for test, seconds in durations.items():
                values[test].append(seconds)
            seen.extend(ids)
        if Counter(seen) != Counter(expected):
            raise ValueError(f"{key}: every discovered test must occur exactly once")
        sources.append(
            {
                field: first[field]
                for field in first
                if field
                not in {
                    "shard",
                    "tests",
                    "discovered",
                    "expected_seconds",
                    "exit_code",
                    "elapsed_seconds",
                }
            }
        )
    return {test: round(statistics.median(values[test]), 6) for test in sorted(values)}, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", nargs="+", type=Path)
    parser.add_argument(
        "--output", type=Path, help="write table and .sources.json; otherwise validate"
    )
    args = parser.parse_args()
    samples = []
    for directory in args.samples:
        for path in sorted(directory.rglob("manifest.json")):
            rows = [
                json.loads(line)
                for line in path.with_name("durations.jsonl").read_text().splitlines()
            ]
            samples.append((json.loads(path.read_text()), rows))
    found = integration_jobs.run_tests.discover(
        integration_jobs.ROOT, integration_jobs.directories(integration_jobs.ROOT)
    )
    expected = {test.id() for test in integration_jobs.tests(found)}
    durations, sources = aggregate(samples, expected)
    if args.output:
        args.output.write_text(json.dumps(durations, indent=2, sort_keys=True) + "\n")
        args.output.with_suffix(".sources.json").write_text(
            json.dumps(
                {
                    "aggregation": "per-test median across complete successful attempts",
                    "sources": sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    print(f"validated {len(sources)} complete attempts, {len(durations)} tests")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from error
