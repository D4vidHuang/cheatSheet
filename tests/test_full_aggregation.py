"""Full-plan audits retain failed/missing requests and reject mixed provenance."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from swe_refactor.core import (digest, load_prepared, make_protocol, prepare,
                               read_json, run_generation, write_json)

SPEC = importlib.util.spec_from_file_location(
    "aggregate_full_run", Path(__file__).resolve().parents[1] / "scripts/aggregate_full_run.py")
aggregator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregator)

CONFIG = {
    "schema_version": 1, "context_policy": "stored_before_file_only",
    "task_mode": "instruction_conditioned_refactoring", "max_model_len": 32768,
    "max_input_tokens": 24576, "max_output_tokens": 8192, "temperature": 0.0,
    "top_p": 1.0, "seed": 42, "enable_thinking": False, "dtype": "bfloat16",
    "quantization": None, "completions_per_task": 1, "prompt_version": "java-files-json-v1",
}
LOCK = {"schema_version": 1, "models": {
    "small": {"model_id": "example/small", "revision": "a" * 40},
    "large": {"model_id": "example/large", "revision": "b" * 40}}}


class GoldBackend:
    """Synthetic correctness control; never offered by the production CLI."""
    def __init__(self, gold):
        self.gold = gold

    def encode(self, messages):
        before = json.loads(messages[1]["content"])["before_files"][0]["path"]
        index = int(Path(before).stem[1:])
        self.text = json.dumps({"files": self.gold[index]})
        return json.dumps(messages), [1] * 4

    def complete(self, token_ids):
        return {"text": self.text, "token_ids": [2] * 3, "finish_reason": "stop",
                "engine_initialization_seconds": 0.0}


class FullAggregationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prepared = self.root / "prepared/swe-refactor"
        rows = []
        self.gold = {}
        for index in range(6):
            prefix = "src/test/java" if index % 2 else "src/main/java"
            before_path = f"{prefix}/A{index}.java"
            after = f"class A{index} {{ int helper() {{ return {index}; }} }}"
            self.gold[index] = [{"path": before_path, "content": after}]
            rows.append({"projectName": "first" if index < 4 else "second",
                         "commitId": f"{index:040x}", "uniqueId": f"id-{index}",
                         "type": "Extract Method" if index % 2 else "Inline Method",
                         "description": "Refactor the supplied Java method",
                         "filePathBefore": before_path, "filePathAfter": before_path,
                         "sourceCodeBeforeForWhole": f"class A{index} {{ }}",
                         "sourceCodeAfterForWhole": after})
        source = self.root / "dataset.json"
        write_json(source, rows)
        prepare(source, self.prepared)
        manifest, self.tasks = load_prepared(self.prepared)
        protocol, _ = make_protocol(self.prepared, "small", LOCK, CONFIG, "all", None,
                                    1, .9, False, environment={"test": True},
                                    num_shards=2, shard_index=0)
        self.plan = {"schema_version": 1, "run_id": "test-full", "models": ["small", "large"],
                     "num_shards": 2, "split": "all", "expected_tasks_per_model": 6,
                     "expected_total_requests": 12, "prepared": "prepared/swe-refactor",
                     "run_root": "runs/full/test-full", "task_ids": [t["task_id"] for t in self.tasks],
                     "dataset_manifest_sha256": digest(manifest), "model_lock_sha256": digest(LOCK),
                     "experiment_sha256": digest(CONFIG),
                     "harness_source_sha256": protocol["harness_source_sha256"]}
        write_json(self.root / "configs/models.lock.json", LOCK)
        write_json(self.root / "configs/experiment.json", CONFIG)

    def tearDown(self):
        self.temp.cleanup()

    def run_path(self, model="small", shard=0):
        return self.root / self.plan["run_root"] / model / f"shard-{shard:05d}" / "model"

    def generate_all(self):
        for model in self.plan["models"]:
            for shard in range(self.plan["num_shards"]):
                protocol, tasks = make_protocol(
                    self.prepared, model, LOCK, CONFIG, "all", None, 1, .9, False,
                    environment={"test": True}, num_shards=2, shard_index=shard)
                run_generation(self.run_path(model, shard), protocol, tasks, GoldBackend(self.gold))

    def test_empty_runs_keep_all_denominators_without_performance(self):
        report = aggregator.aggregate(self.plan, self.root)
        self.assertFalse(report["complete"])
        overall = report["all_planned_requests"]
        self.assertEqual(12, overall["denominator"])
        self.assertEqual({"missing": 12}, overall["status_counts"])
        self.assertEqual(0, overall["valid_records"])
        self.assertEqual({"count": 0, "rate": None}, overall["format_valid"])
        self.assertIsNone(overall["costs"]["output_tokens"]["sum"])
        self.assertIsNone(report["functional_success"])
        self.assertEqual(6, report["models"]["small"]["all_selected_tasks"]["denominator"])
        categories = report["models"]["small"]["by_code_category"]
        self.assertEqual(3, categories["test_source"]["denominator"])
        self.assertEqual(3, categories["other_source"]["denominator"])

    def test_completed_diagnostics_and_costs_are_not_functional_scores(self):
        self.generate_all()
        report = aggregator.aggregate(self.plan, self.root)
        self.assertTrue(report["complete"], report["issues"])
        overall = report["all_planned_requests"]
        self.assertEqual({"completed": 12}, overall["status_counts"])
        self.assertEqual({"count": 12, "rate": 1.0}, overall["exact_stored_after_file"])
        self.assertEqual(48, overall["costs"]["input_tokens"]["sum"])
        self.assertEqual(36, overall["costs"]["output_tokens"]["sum"])
        self.assertEqual(0, overall["costs"]["engine_initialization_seconds"]["sum"])
        self.assertIsNone(overall["functional_success"])
        self.assertEqual(0, overall["failure_denominators"]["strict_functional_evaluation_performed"])
        self.assertTrue(report["models"]["large"]["coverage"]["complete_disjoint_partition"])

    def test_corrupt_and_missing_records_stay_in_global_denominator(self):
        self.generate_all()
        files = sorted((self.run_path() / "records").glob("*.json"))
        bad = read_json(files[0])
        bad["output_text"] += "tampered"
        write_json(files[0], bad)
        files[1].unlink()
        report = aggregator.aggregate(self.plan, self.root)
        overall = report["all_planned_requests"]
        self.assertFalse(report["complete"])
        self.assertEqual(12, overall["denominator"])
        self.assertEqual({"completed": 10, "invalid_receipt": 1, "missing": 1}, overall["status_counts"])
        self.assertEqual(10 / 12, overall["exact_stored_after_file"]["rate"])
        self.assertEqual(30, overall["costs"]["output_tokens"]["sum"])
        self.assertEqual(2, overall["failure_denominators"]["without_valid_record"])

    def test_mixed_protocol_is_excluded_and_coverage_fails_closed(self):
        self.generate_all()
        path = self.run_path() / "protocol.json"
        protocol = read_json(path)
        protocol["model_lock_sha256"] = "f" * 64
        write_json(path, protocol)
        report = aggregator.aggregate(self.plan, self.root)
        self.assertFalse(report["complete"])
        self.assertEqual({"completed": 9, "invalid_protocol": 3}, report["all_planned_requests"]["status_counts"])
        self.assertFalse(report["models"]["small"]["coverage"]["complete_disjoint_partition"])
        self.assertEqual(3, len(report["models"]["small"]["coverage"]["missing_task_ids"]))

    def test_extra_records_and_corrupt_saved_artifacts_prevent_completion(self):
        self.generate_all()
        report = aggregator.aggregate(self.plan, self.root)
        self.assertTrue(report["complete"])
        path = self.run_path() / "diagnostics.json"
        saved = read_json(path)
        saved["functional_success"] = 1.0
        write_json(path, saved)
        write_json(self.run_path() / "records/extra.json", {"status": "completed"})
        report = aggregator.aggregate(self.plan, self.root)
        self.assertFalse(report["complete"])
        self.assertTrue(any("diagnostic" in issue for issue in report["issues"]))
        self.assertEqual(["extra"], report["models"]["small"]["shards"][0]["extra_record_ids"])
        self.assertEqual(12, report["all_planned_requests"]["denominator"])

    def test_cli_writes_incomplete_audit_before_nonzero_exit(self):
        plan_path = self.root / "plan.json"
        output = self.root / "reports/audit.json"
        write_json(plan_path, self.plan)
        args = ["--plan", str(plan_path), "--root", str(self.root), "--output", str(output)]
        self.assertEqual(2, aggregator.main(args))
        self.assertEqual(12, read_json(output)["all_planned_requests"]["denominator"])
        self.assertEqual(0, aggregator.main([*args, "--allow-incomplete"]))
        self.plan["expected_total_requests"] = 11
        write_json(plan_path, self.plan)
        self.assertEqual(2, aggregator.main(args))
        self.assertFalse(read_json(output)["complete"])


if __name__ == "__main__":
    unittest.main()
