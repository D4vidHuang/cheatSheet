#!/usr/bin/env python3
"""Audit a frozen, sharded full run without converting queueing into scores.

All planned requests remain in every denominator. Snapshot matches and response
format are diagnostics; independent refactoring/build/test success stays null.
"""
from __future__ import annotations

import argparse
from collections import Counter
import math
from pathlib import Path, PurePosixPath
import sys

# Also support ``python scripts/aggregate_full_run.py`` from an unpacked release.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from swe_refactor.core import (ProtocolError, audit_summaries, digest, file_sha,
                               load_prepared, read_json, score_run, utc_now,
                               validate_run, write_json)


METRICS = ("format_valid", "exact_stored_after_file", "unchanged_before_response")
COST_FIELDS = ("input_tokens", "output_tokens", "tokenization_seconds",
               "generation_seconds", "engine_initialization_seconds", "elapsed_seconds")


def resolve_under(root: Path, value: str) -> Path:
    """Plan paths are relative to the immutable release, without parent escapes."""
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or path.is_absolute()
            or path.as_posix() != value or ".." in path.parts):
        raise ProtocolError("plan paths must be canonical release-relative paths")
    resolved = root.joinpath(*path.parts).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ProtocolError("plan path escapes release root")
    return resolved


def check_plan(plan: dict) -> None:
    if plan.get("schema_version") != 1 or plan.get("split") != "all":
        raise ProtocolError("full run plan requires schema_version=1 and split=all")
    ids, models, shards = plan["task_ids"], plan["models"], plan["num_shards"]
    if (not isinstance(ids, list) or not ids or ids != sorted(set(ids))
            or any(not isinstance(value, str) for value in ids)):
        raise ProtocolError("plan task IDs must be nonempty, sorted and unique")
    if (not isinstance(models, list) or not models or len(models) != len(set(models))
            or any(not isinstance(value, str) or not value or "/" in value
                   or value in {".", ".."} for value in models)):
        raise ProtocolError("plan model keys must be nonempty and unique")
    if type(shards) is not int or not 1 <= shards <= len(ids):
        raise ProtocolError("plan num_shards must be between one and the task count")
    if (plan["expected_tasks_per_model"] != len(ids)
            or plan["expected_total_requests"] != len(ids) * len(models)):
        raise ProtocolError("plan request denominators differ from its task/model lists")
    for key in ("dataset_manifest_sha256", "model_lock_sha256", "experiment_sha256",
                "harness_source_sha256"):
        value = plan[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ProtocolError(f"plan requires a SHA-256 digest for {key}")
    if "plan_sha256" in plan:
        body = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if digest(body) != plan["plan_sha256"]:
            raise ProtocolError("full plan self hash mismatch")


def protocol_errors(protocol: dict, plan: dict, model: str, model_entry: dict,
                    shard_index: int, expected: list[str]) -> list[str]:
    checks = {
        "dataset_manifest_sha256": plan["dataset_manifest_sha256"],
        "model_lock_sha256": plan["model_lock_sha256"],
        "harness_source_sha256": plan["harness_source_sha256"],
        "model_key": model, "split": "all", "limit": None,
        "model": model_entry,
        "mode": "generation", "num_shards": plan["num_shards"],
        "shard_index": shard_index, "selected_task_ids": expected,
        "selected_task_count": len(expected),
    }
    issues = [f"protocol {key} differs from full plan"
              for key, value in checks.items() if protocol.get(key) != value]
    if digest(protocol.get("experiment")) != plan["experiment_sha256"]:
        issues.append("protocol experiment digest differs from full plan")
    return issues


def summarize(rows: list[dict]) -> dict:
    denominator = len(rows)
    observed = sum(row["valid_record"] for row in rows)
    summary = {
        "denominator": denominator,
        "valid_records": observed,
        "nonempty_model_outputs": sum(row["nonempty_output"] for row in rows),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "functional_success": None, "refactoring_success": None,
    }
    for metric in METRICS:
        count = sum(row[metric] for row in rows)
        summary[metric] = {"count": count,
                           "rate": count / denominator if denominator and observed else None}
    summary["failure_denominators"] = {
        "all_planned_requests": denominator,
        "without_valid_record": denominator - observed,
        "without_nonempty_model_output": denominator - summary["nonempty_model_outputs"],
        "valid_record_with_generation_failure": sum(
            row["valid_record"] and row["status"] != "completed" for row in rows),
        "completed_format_valid": summary["format_valid"]["count"],
        "strict_functional_evaluation_performed": 0,
    }
    costs = {}
    for field in COST_FIELDS:
        values = [row[field] for row in rows if row["valid_record"]
                  and isinstance(row.get(field), (int, float))
                  and not isinstance(row[field], bool)
                  and math.isfinite(row[field]) and row[field] >= 0]
        costs[field] = {"measured_records": len(values),
                        "sum": sum(values) if values else None}
    summary["costs"] = costs
    generation = costs["generation_seconds"]["sum"]
    # Summed per-request service times are not allocated GPU wall time. Engine
    # initialization is kept separate and never counted as token generation.
    summary["cost_scope"] = (
        "Sums from valid task receipts only; missing measurements stay null. "
        "generation_seconds excludes reported engine initialization. "
        "These sums are not scheduler wall time or allocated GPU hours.")
    summary["output_tokens_per_generation_second"] = (
        costs["output_tokens"]["sum"] / generation
        if generation and costs["generation_seconds"]["measured_records"] == observed
        and costs["output_tokens"]["measured_records"] == observed else None)
    return summary


def grouped(rows: list[dict], field: str) -> dict:
    return {key: summarize([row for row in rows if row[field] == key])
            for key in sorted({row[field] for row in rows})}


def attempt_inventory(shard_root: Path) -> list[dict]:
    entries = []
    for path in sorted((shard_root / "attempts").rglob("*")):
        if path.is_file() and not path.is_symlink():
            entries.append({"path": path.relative_to(shard_root).as_posix(),
                            "bytes": path.stat().st_size, "sha256": file_sha(path)})
    return entries


def aggregate(plan: dict, root: Path) -> dict:
    root = root.resolve()
    check_plan(plan)
    prepared = resolve_under(root, plan["prepared"])
    run_root = resolve_under(root, plan["run_root"])
    manifest, tasks = load_prepared(prepared)
    if digest(manifest) != plan["dataset_manifest_sha256"]:
        raise ProtocolError("prepared manifest differs from frozen full plan")
    by_id = {task["task_id"]: task for task in tasks}
    if sorted(by_id) != plan["task_ids"]:
        raise ProtocolError("full plan task coverage differs from prepared tasks")
    lock = read_json(resolve_under(root, plan.get("model_lock", "configs/models.lock.json")))
    experiment = read_json(resolve_under(root, plan.get("experiment", "configs/experiment.json")))
    if digest(lock) != plan["model_lock_sha256"] or digest(experiment) != plan["experiment_sha256"]:
        raise ProtocolError("frozen model lock or experiment differs from full plan")
    if set(plan["models"]) - lock["models"].keys():
        raise ProtocolError("full plan model key absent from frozen model lock")
    for path, expected_sha in plan.get("source_files_sha256", {}).items():
        if file_sha(resolve_under(root, path)) != expected_sha:
            raise ProtocolError(f"frozen source file differs from full plan: {path}")

    all_rows, model_reports, issues = [], {}, []
    for model in plan["models"]:
        rows_by_id = {}
        for task_id in plan["task_ids"]:
            task = by_id[task_id]
            is_test = any(part in {"test", "tests"}
                          for item in task["input"]["files"]
                          for part in PurePosixPath(item["path"]).parts)
            rows_by_id[task_id] = {"task_id": task_id, "project": task["project"],
                                   "type": task["input"]["type"],
                                   "code_category": "test_source" if is_test else "other_source",
                                   "status": "missing", "valid_record": False,
                                   "nonempty_output": False,
                                   **{metric: False for metric in METRICS}}
        shard_reports, selected_ids = [], []
        for index in range(plan["num_shards"]):
            expected = plan["task_ids"][index::plan["num_shards"]]
            shard_root = run_root / model / f"shard-{index:05d}"
            run = shard_root / "model"
            entry = {"shard_index": index, "expected_tasks": len(expected),
                     "run": run.relative_to(root).as_posix(), "issues": [],
                     "attempt_evidence": attempt_inventory(shard_root), "complete": False}
            shard_reports.append(entry)
            if not (run / "protocol.json").exists():
                entry["issues"].append("protocol.json absent; no model records accepted")
                continue
            try:
                protocol = read_json(run / "protocol.json")
                entry["protocol_sha256"] = digest(protocol)
                entry["issues"].extend(protocol_errors(protocol, plan, model, lock["models"][model], index, expected))
                if entry["issues"]:
                    for task_id in expected:
                        rows_by_id[task_id]["status"] = "invalid_protocol"
                    continue
                selected_ids.extend(protocol["selected_task_ids"])
                receipt = validate_run(run, tasks)
                # Capture corrupt/stale saved artifacts before score_run refreshes
                # diagnostics from the independently validated task receipts.
                saved_audit = audit_summaries(run, receipt)
                entry["issues"].extend(saved_audit["aggregate_artifact_errors"])
                entry["receipt_sha256"] = receipt["receipt_sha256"]
                entry["extra_record_ids"] = receipt["extra_record_ids"]
                entry["invalid_records"] = receipt["invalid_records"]
                scored = score_run(prepared, run)
                for row in scored["rows"]:
                    target = rows_by_id[row["task_id"]]
                    target.update({key: row[key] for key in ("status", *METRICS)})
                    if row["task_id"] in receipt["records"]:
                        record = read_json(run / "records" / (row["task_id"] + ".json"))
                        target["valid_record"] = True
                        target["nonempty_output"] = bool(record["output_text"])
                        target.update({key: record.get(key) for key in COST_FIELDS})
                entry["status_counts"] = receipt["status_counts"]
                entry["valid_records"] = receipt["valid_records"]
                entry["complete"] = receipt["complete"] and not entry["issues"]
            except (ProtocolError, OSError, ValueError, KeyError, TypeError) as exc:
                entry["issues"].append(f"{type(exc).__name__}: {exc}")
                for task_id in expected:
                    rows_by_id[task_id]["status"] = "invalid_protocol"
                    rows_by_id[task_id]["valid_record"] = False
                    rows_by_id[task_id]["nonempty_output"] = False
                    for metric in METRICS:
                        rows_by_id[task_id][metric] = False

        coverage_counts = Counter(selected_ids)
        coverage = {"expected_tasks": len(plan["task_ids"]),
                    "accepted_protocol_task_count": len(selected_ids),
                    "accepted_protocol_unique_tasks": len(coverage_counts),
                    "missing_task_ids": sorted(set(plan["task_ids"]) - coverage_counts.keys()),
                    "duplicate_task_ids": sorted(key for key, count in coverage_counts.items() if count > 1),
                    "extra_task_ids": sorted(coverage_counts.keys() - set(plan["task_ids"]))}
        coverage["complete_disjoint_partition"] = not any(
            coverage[key] for key in ("missing_task_ids", "duplicate_task_ids", "extra_task_ids"))
        rows = [rows_by_id[task_id] for task_id in plan["task_ids"]]
        model_reports[model] = {"all_selected_tasks": summarize(rows),
                                "by_project": grouped(rows, "project"),
                                "by_type": grouped(rows, "type"),
                                "by_code_category": grouped(rows, "code_category"),
                                "coverage": coverage, "shards": shard_reports,
                                "complete": coverage["complete_disjoint_partition"]
                                and all(shard["complete"] for shard in shard_reports)}
        all_rows.extend(rows)
        for shard in shard_reports:
            issues.extend(f"{model}/shard-{shard['shard_index']:05d}: {issue}"
                          for issue in shard["issues"])

    report = {"schema_version": 1, "checked_at_utc": utc_now(),
              "run_id": plan["run_id"],
              "plan_sha256": plan.get("plan_sha256", digest(plan)),
              "dataset_manifest_sha256": plan["dataset_manifest_sha256"],
              "harness_source_sha256": plan["harness_source_sha256"],
              "model_lock_sha256": plan["model_lock_sha256"],
              "experiment_sha256": plan["experiment_sha256"],
              "all_planned_requests": summarize(all_rows), "models": model_reports,
              "issues": issues, "complete": all(item["complete"] for item in model_reports.values()),
              "functional_success": None, "refactoring_success": None,
              "metric_scope": (
                  "Diagnostic only: exact match concerns one stored AFTER file, not a complete patch. "
                  "No behavioral/build/test/refactoring score is inferred. All planned requests, "
                  "including missing, corrupt and failed records, remain in denominators. "
                  "When no valid task receipts exist, diagnostic rates and costs are null.")}
    report["report_sha256"] = digest(report)
    return report


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--plan", type=Path, required=True)
    cli.add_argument("--root", type=Path, default=Path.cwd(), help="immutable release root")
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--allow-incomplete", action="store_true", help="save incomplete audit and return zero")
    args = cli.parse_args(argv)
    plan = None
    try:
        plan = read_json(args.plan)
        report = aggregate(plan, args.root.resolve())
    except (ProtocolError, OSError, ValueError, KeyError, TypeError) as exc:
        report = {"schema_version": 1, "checked_at_utc": utc_now(), "complete": False,
                  "issues": [f"{type(exc).__name__}: {exc}"],
                  "expected_total_requests": plan.get("expected_total_requests")
                  if isinstance(plan, dict) else None,
                  "functional_success": None, "refactoring_success": None}
        report["report_sha256"] = digest(report)
    write_json(args.output, report)
    summary = report.get("all_planned_requests", {})
    print(f"audit={args.output} complete={report['complete']} "
          f"valid_records={summary.get('valid_records', 0)}/"
          f"{summary.get('denominator', report.get('expected_total_requests', '?'))}")
    return 0 if report["complete"] or args.allow_incomplete else 2


if __name__ == "__main__":
    raise SystemExit(main())
