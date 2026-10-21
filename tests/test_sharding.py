import copy
import json
import tempfile
import unittest
from pathlib import Path

from swe_refactor.cli import parser
from swe_refactor.core import (ProtocolError, digest, load_prepared, make_protocol,
                               open_run, prepare, read_json, read_jsonl,
                               reconcile_resume_protocol, run_generation,
                               select_tasks, validate_run, validate_shard_coverage,
                               write_json)


CONFIG = {
    "schema_version": 1, "context_policy": "stored_before_file_only",
    "task_mode": "instruction_conditioned_refactoring", "max_model_len": 32768,
    "max_input_tokens": 24576, "max_output_tokens": 8192, "temperature": 0.0,
    "top_p": 1.0, "seed": 42, "enable_thinking": False, "dtype": "bfloat16",
    "quantization": None, "completions_per_task": 1, "prompt_version": "java-files-json-v1",
}
LOCK = {"schema_version": 1, "models": {"fixture": {
    "model_id": "example/fixture", "revision": "a" * 40}}}
ENVIRONMENT = {
    "python": "3.11.9", "platform": "Linux-6.8.0-x86_64",
    "packages": {"vllm": "0.fixture", "torch": "2.fixture"},
    "gpu_inventory": "NVIDIA A100-SXM4-80GB, GPU-old-uuid, 81920 MiB, 535.104.05",
    "cuda_visible_devices": "0", "vllm_use_flashinfer_sampler": "0",
}


class Backend:
    def __init__(self):
        self.calls = 0

    def encode(self, messages):
        return json.dumps(messages), [1]

    def complete(self, token_ids):
        self.calls += 1
        return {"text": '{"files":[{"path":"src/A.java","content":"class A {}"}]}',
                "token_ids": [2], "finish_reason": "stop"}


class ShardingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "source.json"
        rows = [{"projectName": "fixture", "commitId": f"{i:040x}",
                 "uniqueId": str(i), "type": "Extract Method", "description": "Extract f",
                 "filePathBefore": f"src/A{i}.java", "filePathAfter": f"src/A{i}.java",
                 "sourceCodeBeforeForWhole": f"class A{i} {{}}",
                 "sourceCodeAfterForWhole": f"class A{i} {{ void f() {{}} }}"}
                for i in range(29)]
        write_json(source, rows)
        self.prepared = self.root / "prepared"
        prepare(source, self.prepared)
        _, self.tasks = load_prepared(self.prepared)

    def tearDown(self):
        self.temp.cleanup()

    def protocol(self, count=1, index=0, split="all", limit=None, environment=None):
        return make_protocol(self.prepared, "fixture", LOCK, CONFIG, split, limit,
                             1, .9, False, environment=environment or ENVIRONMENT,
                             num_shards=count, shard_index=index)

    def test_disjoint_balanced_shards_cover_every_task_and_ignore_row_order(self):
        for count in (1, 2, 4, 7, 29):
            shards = [select_tasks(self.tasks, "all", num_shards=count, shard_index=index)
                      for index in range(count)]
            ids = [task["task_id"] for shard in shards for task in shard]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(sorted(ids), [task["task_id"] for task in self.tasks])
            self.assertLessEqual(max(map(len, shards)) - min(map(len, shards)), 1)
            for index, shard in enumerate(shards):
                self.assertEqual(shard, select_tasks(self.tasks[::-1], "all",
                                                    num_shards=count, shard_index=index))

    def test_split_precedes_sharding_and_limit_follows_sharding(self):
        eligible = [task for task in self.tasks if task["split"] == "test"]
        for index in range(4):
            selected = select_tasks(self.tasks, "test", 2, 4, index)
            self.assertEqual(eligible[index::4][:2], selected)
        self.assertEqual(self.tasks[:3], select_tasks(self.tasks, "all", 3))

    def test_invalid_shard_coordinates_and_duplicate_sources_fail_closed(self):
        for count, index in ((0, 0), (-1, 0), (2, -1), (2, 2), (30, 0),
                             (True, 0), (1, False), (1.5, 0)):
            with self.subTest(count=count, index=index), self.assertRaises(ProtocolError):
                select_tasks(self.tasks, "all", num_shards=count, shard_index=index)
        with self.assertRaises(ProtocolError):
            select_tasks(self.tasks + self.tasks[:1], "all")
        with self.assertRaises(ProtocolError):
            select_tasks(self.tasks, "unknown")

    def test_full_plan_coverage_rejects_missing_duplicate_tampered_and_mixed_shards(self):
        protocols = [self.protocol(4, index)[0] for index in range(4)]
        receipt = validate_shard_coverage(self.tasks, protocols)
        self.assertTrue(receipt["complete"])
        self.assertEqual(29, receipt["expected_tasks"])
        self.assertEqual(29, sum(receipt["shard_task_counts"].values()))
        for altered in (protocols[:-1], protocols[:-1] + protocols[:1]):
            with self.assertRaises(ProtocolError):
                validate_shard_coverage(self.tasks, altered)
        for field, replacement in (("selected_task_ids", []),
                                   ("selected_task_count", 1),
                                   ("selected_task_sha256", {}),
                                   ("harness_source_sha256", "different"),
                                   ("limit", 1)):
            altered = copy.deepcopy(protocols)
            altered[0][field] = replacement
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                validate_shard_coverage(self.tasks, altered)

    def test_exact_shard_ids_are_frozen_and_validation_reconstructs_membership(self):
        protocol, selected = self.protocol(4, 2)
        run = self.root / "run"
        run_generation(run, protocol, selected, Backend())
        self.assertEqual([task["task_id"] for task in self.tasks[2::4]],
                         protocol["selected_task_ids"])
        self.assertTrue(validate_run(run, self.tasks)["complete"])
        altered = copy.deepcopy(protocol)
        altered["shard_index"] = 1
        write_json(run / "protocol.json", altered)
        with self.assertRaisesRegex(ProtocolError, "frozen split and shard"):
            validate_run(run, self.tasks)

    def test_protocol_schema_and_selection_policy_cannot_silently_change(self):
        protocol, _ = self.protocol()
        self.assertIs(protocol["engine_language_model_only"], True)
        for field, value in (("schema_version", 999), ("schema_version", True),
                             ("shard_selection_policy", "unknown")):
            changed = copy.deepcopy(protocol)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                open_run(self.root / "invalid", changed)
        legacy = copy.deepcopy(protocol)
        legacy["schema_version"] = 1
        for key in ("num_shards", "shard_index", "shard_selection_policy"):
            del legacy[key]
        run = self.root / "legacy"
        open_run(run, legacy)
        self.assertEqual(29, validate_run(run, self.tasks)["expected_tasks"])
        config = {**CONFIG, "language_model_only": False}
        with self.assertRaisesRegex(ProtocolError, "language_model_only"):
            make_protocol(self.prepared, "fixture", LOCK, config, "all", None,
                          1, .9, False, environment=ENVIRONMENT)

    def test_cli_shards_default_to_the_previous_unsharded_selection(self):
        defaults = parser().parse_args(["generate", "--model", "fixture", "--run", "somewhere"])
        self.assertEqual((1, 0, False), (defaults.num_shards, defaults.shard_index,
                                        defaults.resume_equivalent_hardware))
        args = parser().parse_args(["generate", "--model", "fixture", "--run", "somewhere",
                                    "--num-shards", "7", "--shard-index", "6",
                                    "--resume-equivalent-hardware"])
        self.assertEqual((7, 6, True), (args.num_shards, args.shard_index,
                                       args.resume_equivalent_hardware))

    def test_gpu_identity_resume_is_explicit_and_keeps_original_receipts(self):
        protocol, tasks = self.protocol(limit=3)
        run = self.root / "resume"
        first_backend = Backend()
        run_generation(run, protocol, tasks, first_backend)
        original_record = read_json(run / "records" / (tasks[0]["task_id"] + ".json"))
        candidate = copy.deepcopy(protocol)
        candidate["environment"]["gpu_inventory"] = ENVIRONMENT["gpu_inventory"].replace(
            "GPU-old-uuid", "GPU-new-uuid")
        candidate["environment"]["cuda_visible_devices"] = "3"
        with self.assertRaisesRegex(ProtocolError, "resume refused"):
            reconcile_resume_protocol(run, candidate)
        frozen, actual_environment = reconcile_resume_protocol(run, candidate, True)
        self.assertEqual(protocol, frozen)
        self.assertEqual(candidate["environment"], actual_environment)
        backend = Backend()
        run_generation(run, frozen, tasks, backend, attempt_environment=actual_environment)
        self.assertEqual(0, backend.calls)
        self.assertEqual(original_record, read_json(run / "records" / (tasks[0]["task_id"] + ".json")))
        self.assertEqual(protocol, read_json(run / "protocol.json"))
        events = [row for row in read_jsonl(run / "ledger.jsonl")
                  if row["event"] == "equivalent_hardware_resume"]
        self.assertEqual(1, len(events))
        self.assertEqual(digest(actual_environment), events[0]["environment_sha256"])
        self.assertEqual(actual_environment, events[0]["environment"])

    def test_equivalent_resume_rejects_hardware_driver_software_and_protocol_drift(self):
        protocol, _ = self.protocol()
        run = self.root / "resume"
        open_run(run, protocol)
        candidates = []
        for old, new in (("A100", "H100"), ("81920", "40960"),
                         ("535.104.05", "550.10")):
            changed = copy.deepcopy(protocol)
            changed["environment"]["gpu_inventory"] = changed["environment"]["gpu_inventory"].replace(old, new)
            candidates.append(changed)
        for key, value in (("python", "3.12.0"), ("platform", "Linux-new-kernel"),
                           ("packages", {"vllm": "changed"}),
                           ("cuda_visible_devices", "0,1"), ("gpu_inventory", None)):
            changed = copy.deepcopy(protocol)
            changed["environment"][key] = value
            candidates.append(changed)
        for key, value in (("harness_source_sha256", "new source"), ("limit", 1),
                           ("model", {"revision": "b" * 40})):
            changed = copy.deepcopy(protocol)
            changed[key] = value
            candidates.append(changed)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ProtocolError):
                reconcile_resume_protocol(run, candidate, True)


if __name__ == "__main__":
    unittest.main()
