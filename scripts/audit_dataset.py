#!/usr/bin/env python3
"""Audit the supplied SWE-Refactor JSON without executing upstream code.

The report contains metadata, lengths and hashes, never Java source bodies.
Only the Python standard library is required. Repeated runs on unchanged inputs
produce byte-identical reports (there are no timestamps or absolute paths).
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = REPO_ROOT / "data/SWE-Refactor/pure_refactoring_data.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def counts(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def identity(row: dict[str, Any]) -> dict[str, str]:
    return {key: row[key] for key in ("projectName", "uniqueId", "type")}


def length_stats(values: list[str]) -> dict[str, int]:
    lengths = sorted(len(value) for value in values)
    result = {"min": lengths[0], "max": lengths[-1]}
    for label, quantile in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        result[label] = lengths[int((len(lengths) - 1) * quantile)]
    return result


def duplicate_summary(keys: list[Any]) -> dict[str, int]:
    multiplicities = Counter(keys)
    return {
        "distinct": len(multiplicities),
        "duplicate_excess": len(keys) - len(multiplicities),
        "groups_with_duplicates": sum(value > 1 for value in multiplicities.values()),
        "max_group_size": max(multiplicities.values(), default=0),
    }


def is_test_path(path: str) -> bool:
    return any(part in {"test", "tests"} for part in Path(path).parts)


def audit(data_path: Path) -> dict[str, Any]:
    rows = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Expected a nonempty JSON array of task objects")
    total = len(rows)
    keys = sorted({key for row in rows for key in row})
    schema = {
        key: {
            "present": sum(key in row for row in rows),
            "missing": sum(key not in row for row in rows),
            "null": sum(key in row and row[key] is None for row in rows),
            "empty": sum(key in row and row[key] in ("", [], {}) for row in rows),
            "types": counts(type(row[key]).__name__ for row in rows if key in row),
        }
        for key in keys
    }
    by_id = {row["uniqueId"]: row for row in rows}
    by_project = counts(row["projectName"] for row in rows)
    by_type = counts(row["type"] for row in rows)

    commands: list[dict[str, Any]] = []
    for command, count in sorted(Counter(row["compileCommand"] for row in rows).items()):
        malformed = False
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = []
            malformed = True
        skips_tests = any(token in {"-DskipTests", "-DskipTests=true", "-Dmaven.test.skip=true"} for token in tokens)
        skips_tests = skips_tests or bool(re.search(r"(?:^|\s)-x\s+test(?:\s|$)", command))
        commands.append({
            "command": command,
            "tasks": count,
            "projects": sorted({row["projectName"] for row in rows if row["compileCommand"] == command}),
            "shell_tokenization_error": malformed,
            "explicitly_skips_tests": skips_tests,
            "contains_test_exclusions": any(token.startswith("-Dtest=") and "!" in token for token in tokens),
        })

    replacement_hazards = []
    for row in rows:
        before = row["sourceCodeBeforeRefactoring"]
        occurrences = row["sourceCodeBeforeForWhole"].count(before) if before else None
        if not before or occurrences != 1:
            replacement_hazards.append({
                **identity(row),
                "reason": "empty_before_snippet" if not before else "nonunique_before_snippet",
                "snippet_characters": len(before),
                "occurrences": occurrences,
            })

    split_files = []
    split_ids: set[str] = set()
    for split in sorted((data_path.parent / "code/data").glob("*/*_pure_refactoring_data.json")):
        subset = json.loads(split.read_text(encoding="utf-8"))
        ids = {row["uniqueId"] for row in subset}
        split_ids.update(ids)
        split_files.append({
            "path": display_path(split),
            "sha256": sha256(split),
            "tasks": len(subset),
            "projects": counts(row.get("projectName", split.parent.name) for row in subset),
            "ids_absent_from_canonical": sorted(ids - set(by_id)),
            "records_differing_from_canonical": sum(row != by_id.get(row["uniqueId"]) for row in subset),
        })

    examples = []
    for ref_type in by_type:
        candidates = [row for row in rows if row["type"] == ref_type and not is_test_path(row["filePathBefore"])]
        row = candidates[0] if candidates else next(row for row in rows if row["type"] == ref_type)
        examples.append({
            **identity(row),
            **{key: row[key] for key in ("description", "filePathBefore", "filePathAfter", "methodNameBefore")},
        })

    result: dict[str, Any] = {
        "audit_schema_version": 1,
        "dataset": {"path": display_path(data_path), "bytes": data_path.stat().st_size, "sha256": sha256(data_path)},
        "scope": "Local supplied files only; upstream metadata is not live compilation/test evidence.",
        "tasks": total,
        "java_path_tasks": sum(row["filePathBefore"].endswith(".java") and row["filePathAfter"].endswith(".java") for row in rows),
        "projects": len(by_project),
        "distinct_project_commit_pairs": len({(row["projectName"], row["commitId"]) for row in rows}),
        "by_project": by_project,
        "by_type": by_type,
        "by_project_and_type": {project: counts(row["type"] for row in rows if row["projectName"] == project) for project in by_project},
        "jdk": counts(row["compileJDK"] for row in rows),
        "metadata_flags": {key: counts(row.get(key, "<missing>") for row in rows) for key in ("isPureRefactoring", "moveFileExist", "compileResultBefore", "compileResultCurrent", "testResult", "hasTestC")},
        "schema": schema,
        "input_size_characters": {key: length_stats([row[key] for row in rows]) for key in ("sourceCodeBeforeRefactoring", "sourceCodeBeforeForWhole", "sourceCodeAfterRefactoring", "sourceCodeAfterForWhole", "callInfo")},
        "coverage": {
            "test_source_path_tasks": sum(is_test_path(row["filePathBefore"]) for row in rows),
            "testMethod_marker_tasks": sum("testMethod" in row["coverageInfo"] for row in rows),
            "jacoco_counter_tasks": sum("LINE" in row["coverageInfo"] for row in rows),
            "counter_key_patterns": counts(",".join(sorted(row["coverageInfo"])) for row in rows),
        },
        "context": {
            "callInfo_available": sum(row["callInfo"] not in ("", "N/A", None) for row in rows),
            "callInfo_NA": sum(row["callInfo"] == "N/A" for row in rows),
            "callGraph_present": sum("callGraph" in row for row in rows),
            "different_before_after_file_paths": sum(row["filePathBefore"] != row["filePathAfter"] for row in rows),
        },
        "duplicates": {
            "uniqueId": duplicate_summary([row["uniqueId"] for row in rows]),
            "project_uniqueId": duplicate_summary([(row["projectName"], row["uniqueId"]) for row in rows]),
            "project_commit_before_file_and_source": duplicate_summary([(row["projectName"], row["commitId"], row["filePathBefore"], row["sourceCodeBeforeForWhole"]) for row in rows]),
            "project_before_snippet": duplicate_summary([(row["projectName"], row["sourceCodeBeforeRefactoring"]) for row in rows]),
        },
        "anomalies": {
            "replacement_hazards": replacement_hazards,
            "upstream_after_compile_false": [identity(row) for row in rows if row["compileResultCurrent"] is False],
            "same_before_after_whole_source": sum(row["sourceCodeBeforeForWhole"] == row["sourceCodeAfterForWhole"] for row in rows),
            "malformed_build_command_tasks": sum(command["tasks"] for command in commands if command["shell_tokenization_error"]),
            "explicit_test_skip_command_tasks": sum(command["tasks"] for command in commands if command["explicitly_skips_tests"]),
        },
        "build_commands": commands,
        "per_project_copies": {
            "files": split_files,
            "task_rows": sum(split["tasks"] for split in split_files),
            "distinct_ids": len(split_ids),
            "canonical_tasks_missing_from_copies": len(set(by_id) - split_ids),
            "canonical_projects_missing_from_copies": sorted({row["projectName"] for row in rows if row["uniqueId"] not in split_ids}),
        },
        "examples": examples,
        "evidence_notes": [
            "description names developer-selected target operations/methods/classes; using it changes operation-only reproduction into guided refactoring.",
            "after-source and diff fields contain reference answers and must not enter model prompts or retrieval indexes.",
            "diffLocations/uniqueId contain coordinates from the real change; treat as evaluator metadata unless explicitly defining an oracle-location condition.",
            "coverageInfo.testMethod is assigned covered=1 in pre_process_data.py when a test method is detected; it is not measured production-method coverage.",
            "compileResultBefore/Current/testResult/hasTestC are imported metadata, not results of this audit.",
            "Multiple task IDs share a commit/source snapshot; preserve commit groups in any development/test split and paired uncertainty analysis.",
            "A changed file path is not a necessary condition for Move Method: methods can move between classes nested in one Java file.",
        ],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "reports/dataset_audit.json")
    args = parser.parse_args()
    result = audit(args.data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Audited {result['tasks']} Java tasks across {result['projects']} projects")
    print(f"SHA-256: {result['dataset']['sha256']}")
    print(f"Report: {display_path(args.output)}")


if __name__ == "__main__":
    main()
