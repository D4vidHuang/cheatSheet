import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swe_refactor.core import (ProtocolError, digest, file_sha, make_protocol, prepare,
                               read_json, run_generation, source_fingerprint, write_json)

SPEC = importlib.util.spec_from_file_location(
    "full_array_worker", Path(__file__).parents[1] / "scripts/full_array_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

CONFIG = {
    "schema_version": 1, "context_policy": "stored_before_file_only",
    "task_mode": "instruction_conditioned_refactoring", "max_model_len": 32768,
    "max_input_tokens": 24576, "max_output_tokens": 8192, "temperature": 0.0,
    "top_p": 1.0, "seed": 42, "enable_thinking": False, "dtype": "bfloat16",
    "quantization": None, "completions_per_task": 1, "prompt_version": "java-files-json-v1",
}
LOCK = {"schema_version": 1, "models": {
    "test-model": {"model_id": "example/test", "revision": "a" * 40}}}


class FailingBackend:
    def encode(self, messages):
        return "prompt", [1]

    def complete(self, token_ids):
        raise RuntimeError("independent test simulates unavailable CUDA kernel")


class FullWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "configs").mkdir()
        self.prepared = self.root / "prepared/swe-refactor"
        rows = [{"projectName": "example", "commitId": str(i), "uniqueId": str(i),
                 "type": "Extract Method", "description": "Extract helper",
                 "filePathBefore": "src/A.java", "filePathAfter": "src/A.java",
                 "sourceCodeBeforeForWhole": "class A {}",
                 "sourceCodeAfterForWhole": "class A { void helper() {} }"}
                for i in range(5)]
        write_json(self.root / "dataset.json", rows)
        self.manifest = prepare(self.root / "dataset.json", self.prepared)
        write_json(self.root / "configs/models.lock.json", LOCK)
        write_json(self.root / "configs/experiment.json", CONFIG)
        self.plan = {
            "schema_version": 1, "run_id": "example", "split": "all",
            "models": ["test-model"], "num_shards": 2,
            "prepared": "prepared/swe-refactor", "run_root": "runs/full/example",
            "hashes": {"dataset_manifest_sha256": digest(self.manifest),
                       "model_lock_sha256": digest(LOCK), "experiment_sha256": digest(CONFIG),
                       "harness_source_sha256": source_fingerprint()},
        }
        self.shard = self.root / "shard"
        self.attempt = self.shard / "attempts/current"
        self.attempt.mkdir(parents=True)
        self.owner = {"slurm_job_id": "123", "slurm_restart_count": 0}
        self.current = {"slurm_job_id": "456", "slurm_restart_count": 0}

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_freeze_detects_data_config_and_source_changes(self):
        _, first = worker.verify_plan(self.root, self.plan, "test-model", 0)
        _, second = worker.verify_plan(self.root, self.plan, "test-model", 1)
        self.assertEqual(5, len(first) + len(second))
        self.assertFalse({x["task_id"] for x in first} & {x["task_id"] for x in second})
        self.plan["source_files_sha256"] = {"dataset.json": file_sha(self.root / "dataset.json")}
        worker.verify_plan(self.root, self.plan, "test-model", 0)
        (self.root / "dataset.json").write_text("changed")
        with self.assertRaisesRegex(ProtocolError, "source hash mismatch"):
            worker.verify_plan(self.root, self.plan, "test-model", 0)
        self.plan.pop("source_files_sha256")
        write_json(self.root / "configs/experiment.json", {**CONFIG, "seed": 99})
        with self.assertRaisesRegex(ProtocolError, "experiment_sha256"):
            worker.verify_plan(self.root, self.plan, "test-model", 0)
        write_json(self.root / "configs/experiment.json", CONFIG)
        with (self.prepared / "tasks.jsonl").open("a") as handle:
            handle.write("{}\n")
        with self.assertRaisesRegex(ProtocolError, "tasks hash mismatch"):
            worker.verify_plan(self.root, self.plan, "test-model", 0)

    def test_plan_self_hash_and_unsafe_paths_are_rejected(self):
        self.plan["plan_sha256"] = digest(self.plan)
        worker.verify_plan(self.root, self.plan, "test-model", 0)
        self.plan["num_shards"] = 3
        with self.assertRaisesRegex(ProtocolError, "self hash mismatch"):
            worker.verify_plan(self.root, self.plan, "test-model", 0)
        for name in ("../escape", "/tmp/escape", "x/../escape", "x//escape"):
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                worker.confined(self.root, name)
        (self.root / "escape-link").symlink_to(self.root.parent)
        with self.assertRaisesRegex(ProtocolError, "escapes"):
            worker.confined(self.root, "escape-link/file")

    def test_flat_submission_plan_interface_and_task_coverage_are_verified(self):
        flat = {key: value for key, value in self.plan.items() if key != "hashes"}
        flat.update(self.plan["hashes"])
        worker.verify_plan(self.root, flat, "test-model", 0)
        flat["task_ids"] = ["missing-task"]
        with self.assertRaisesRegex(ProtocolError, "task coverage"):
            worker.verify_plan(self.root, flat, "test-model", 0)

    def test_worker_lock_never_removes_an_unverified_owner(self):
        write_json(self.shard / "worker.lock", self.owner)
        with mock.patch.object(worker, "scheduler_job_terminal", return_value=False):
            with self.assertRaisesRegex(ProtocolError, "unverified"):
                worker.acquire_worker_lock(self.shard, self.current, self.attempt)
        self.assertEqual(self.owner, read_json(self.shard / "worker.lock"))
        with mock.patch.object(worker, "scheduler_job_terminal", return_value=True):
            guard = worker.acquire_worker_lock(self.shard, self.current, self.attempt)
        self.assertEqual(self.current, read_json(guard))
        self.assertEqual(self.owner, read_json(self.attempt / "recovered-worker-lock.json"))

    def test_same_allocation_requires_a_later_scheduler_restart(self):
        self.assertFalse(worker.owner_stopped(self.owner, self.owner))
        self.assertTrue(worker.owner_stopped(self.owner, {
            "slurm_job_id": "123", "slurm_restart_count": 1}))
        self.assertFalse(worker.owner_stopped({"slurm_job_id": "123"}, {
            "slurm_job_id": "123", "slurm_restart_count": 1}))

    def test_generation_lock_requires_owner_and_scheduler_proof(self):
        run = self.shard / "model"
        run.mkdir()
        (run / "active.lock").write_text("pid=old time=old\n")
        with self.assertRaisesRegex(ProtocolError, "unverified"):
            worker.recover_core_lock(run, self.current, self.attempt)
        write_json(self.shard / "model-owner.json", self.owner)
        with mock.patch.object(worker, "scheduler_job_terminal", return_value=False):
            with self.assertRaises(ProtocolError):
                worker.recover_core_lock(run, self.current, self.attempt)
        self.assertTrue((run / "active.lock").exists())
        with mock.patch.object(worker, "scheduler_job_terminal", return_value=True):
            worker.recover_core_lock(run, self.current, self.attempt)
        self.assertFalse((run / "active.lock").exists())
        self.assertIn("pid=old", read_json(self.attempt / "recovered-generation-lock.json")["lock_text"])

    def test_scheduler_steps_cannot_prove_parent_allocation_ended(self):
        for text, expected in (("123.batch|COMPLETED|\n", False),
                               ("123|RUNNING|\n123.batch|COMPLETED|\n", False),
                               ("123|CANCELLED by 777|\n", True),
                               ("123|COMPLETED|\n", True)):
            completed = subprocess.CompletedProcess([], 0, text, "")
            with self.subTest(text=text), mock.patch.object(
                    worker.subprocess, "run", return_value=completed):
                self.assertEqual(expected, worker.scheduler_job_terminal("123"))

    def test_all_engine_errors_are_complete_receipts_but_failed_worker_diagnostics(self):
        protocol, tasks = make_protocol(self.prepared, "test-model", LOCK, CONFIG,
                                        "all", None, 1, .9, False, {}, num_shards=2,
                                        shard_index=0)
        run = self.shard / "model"
        run_generation(run, protocol, tasks, FailingBackend())
        result = worker.diagnostics(self.prepared, run, self.attempt)
        self.assertTrue(result["receipt_complete"])
        self.assertTrue(result["all_generation_error"])
        self.assertEqual({"generation_error": len(tasks)}, result["status_counts"])
        self.assertIsNone(read_json(self.attempt / "score.json")["functional_success"])

    def test_batch_signal_terminates_generation_process_group(self):
        interrupted = {"signal": signal.SIGTERM, "child_pid": None}
        result = worker.run_child([sys.executable, "-c", "import time; time.sleep(60)"],
                                  self.attempt / "runner.log", interrupted)
        self.assertEqual(-signal.SIGTERM, result)
        with self.assertRaises(ProcessLookupError):
            os.kill(interrupted["child_pid"], 0)


if __name__ == "__main__":
    unittest.main()
