import copy
import json
import tempfile
import unittest
from pathlib import Path

from swe_refactor.core import (ProtocolError, audit_summaries, digest, export_predictions,
                               generate_one, load_prepared, make_protocol, messages_for,
                               open_run, parse_output, prepare, read_json, read_jsonl,
                               run_generation, score_run, validate_run, write_json)


CONFIG = {
    "schema_version": 1, "context_policy": "stored_before_file_only",
    "task_mode": "instruction_conditioned_refactoring", "max_model_len": 32768,
    "max_input_tokens": 24576, "max_output_tokens": 8192, "temperature": 0.0,
    "top_p": 1.0, "seed": 42, "enable_thinking": False, "dtype": "bfloat16",
    "quantization": None, "completions_per_task": 1, "prompt_version": "java-files-json-v1",
}
LOCK = {"schema_version": 1, "models": {"test": {"model_id": "example/test", "revision": "a" * 40}}}


def dataset_row(index, commit=None):
    return {"projectName": "sample", "commitId": commit or f"{index:040x}",
            "uniqueId": f"row-{index}", "type": "Extract Method",
            "description": f"Extract method extracted{index} from original",
            "filePathBefore": f"src/Before{index}.java",
            "sourceCodeBeforeForWhole": f"class Before{index} {{ void original() {{ }} }}",
            "filePathAfter": f"src/After{index}.java",
            "sourceCodeAfterForWhole": f"GOLD_SECRET_{index}",
            "diffSourceCode": f"DIFF_SECRET_{index}", "testResult": "TEST_SECRET",
            "compileJDK": 11, "compileCommand": "mvn test"}


class FakeBackend:
    """Explicit unit-test fixture; not exposed as an inference option."""
    def __init__(self, text='{"files":[{"path":"src/A.java","content":"class A {}"}]}',
                 tokens=100, finish="stop", output_tokens=10, error=None):
        self.text = text
        self.tokens = tokens
        self.finish = finish
        self.output_tokens = output_tokens
        self.error = error
        self.calls = 0

    def encode(self, messages):
        return json.dumps(messages), [1] * self.tokens

    def complete(self, token_ids):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return {"text": self.text, "token_ids": [2] * self.output_tokens,
                "finish_reason": self.finish, "stop_reason": None}


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = self.root / "dataset.json"
        # At least two tasks share a commit and must remain together.
        self.rows = [dataset_row(i, commit="b" * 40 if i in {0, 1} else None)
                     for i in range(24)]
        write_json(self.dataset, self.rows)
        self.prepared = self.root / "prepared"
        prepare(self.dataset, self.prepared)
        self.manifest, self.tasks, self.gold = load_prepared(self.prepared, True)

    def tearDown(self):
        self.temp.cleanup()

    def protocol(self, limit=3, tokenize_only=False):
        return make_protocol(self.prepared, "test", LOCK, CONFIG, "all", limit,
                             1, .9, tokenize_only, environment={"test": True})

    def test_prompt_excludes_gold_and_metadata(self):
        for task in self.tasks:
            serialized = json.dumps(messages_for(task))
            for secret in ("GOLD_SECRET", "DIFF_SECRET", "TEST_SECRET", "commit_id", "upstream_unique_id"):
                self.assertNotIn(secret, serialized)
            self.assertNotIn("After", serialized)
            self.assertIn(task["input"]["description"], serialized)
        self.assertNotIn("GOLD_SECRET", (self.prepared / "tasks.jsonl").read_text())

    def test_grouped_splits_are_disjoint_and_deterministic(self):
        splits = {}
        for task in self.tasks:
            key = (task["project"], task["commit_id"])
            self.assertEqual(splits.setdefault(key, task["split"]), task["split"])
        self.assertEqual({"dev", "pilot", "test"}, set(splits.values()))
        reverse = self.root / "reverse.json"
        write_json(reverse, self.rows[::-1])
        second = self.root / "second"
        prepare(reverse, second)
        self.assertEqual(self.tasks, load_prepared(second)[1])
        # prepare is idempotent but never overwrites another freeze.
        self.assertEqual(self.manifest, prepare(self.dataset, self.prepared))
        with self.assertRaises(ProtocolError):
            prepare(self.dataset, self.prepared, seed=99)

    def test_frozen_source_tampering_is_detected(self):
        with (self.prepared / "tasks.jsonl").open("a") as handle:
            handle.write("{}\n")
        with self.assertRaisesRegex(ProtocolError, "hash mismatch"):
            load_prepared(self.prepared)

    def test_strict_parser_rejects_traversal_duplicates_and_fences(self):
        for path in ("../A.java", "/tmp/A.java", "a/../A.java", "a//A.java",
                     "a\\A.java", "C:/A.java", ".git/A.java", "A.java\n", "pom.xml"):
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                parse_output(json.dumps({"files": [{"path": path, "content": "x"}]}))
        for text in ('```json\n{"files":[]}\n```', '{"files":[],"other":1}',
                     '{"files":[],"files":[]}', '{"files":[]}',
                     '{"files":[{"path":"A.java","content":3}]}',
                     '{"files":[{"path":"A.java","content":"x"},{"path":"A.java","content":null}]}'):
            with self.subTest(text=text), self.assertRaises(ProtocolError):
                parse_output(text)
        self.assertEqual([{"path": "src/A.java", "content": None}],
                         parse_output('{"files":[{"path":"src/A.java","content":null}]}'))

    def test_overflow_records_failure_without_truncation_or_generation(self):
        protocol, tasks = self.protocol(1)
        backend = FakeBackend(tokens=24577)
        result = generate_one(tasks[0], protocol, backend)
        self.assertEqual("out_of_context", result["status"])
        self.assertEqual(24577, result["input_tokens"])
        self.assertEqual(0, backend.calls)
        self.assertEqual(0, result["output_tokens"])
        boundary = FakeBackend(tokens=24576)
        self.assertEqual("completed", generate_one(tasks[0], protocol, boundary)["status"])
        self.assertEqual(1, boundary.calls)

    def test_truncation_invalid_output_and_engine_error_are_explicit(self):
        protocol, tasks = self.protocol(1)
        for backend, status in ((FakeBackend(finish="length"), "truncated"),
                                (FakeBackend(output_tokens=8192), "truncated"),
                                (FakeBackend(text="not-json"), "invalid_output"),
                                (FakeBackend(error="GPU OOM"), "generation_error")):
            with self.subTest(status=status):
                result = generate_one(tasks[0], protocol, backend)
                self.assertEqual(status, result["status"])
                self.assertIsNone(result["files"])

    def test_tokenization_mode_cannot_produce_a_completion(self):
        protocol, tasks = self.protocol(1, tokenize_only=True)
        backend = FakeBackend()
        record = generate_one(tasks[0], protocol, backend)
        self.assertEqual("tokenized_only", record["status"])
        self.assertEqual(0, backend.calls)

    def test_resume_does_not_sample_twice_and_refuses_mixed_provenance(self):
        protocol, tasks = self.protocol(3)
        run = self.root / "run"
        backend = FakeBackend()
        receipt = run_generation(run, protocol, tasks, backend)
        self.assertTrue(receipt["complete"])
        self.assertEqual(3, backend.calls)
        self.assertEqual(receipt, run_generation(run, protocol, tasks, backend))
        self.assertEqual(3, backend.calls)
        for field in ("model", "environment", "limit", "harness_source_sha256"):
            changed = copy.deepcopy(protocol)
            changed[field] = "changed"
            with self.subTest(field=field), self.assertRaisesRegex(ProtocolError, "resume refused"):
                open_run(run, changed)

    def test_missing_and_invalid_receipts_stay_in_scoring_denominator(self):
        protocol, tasks = self.protocol(3)
        run = self.root / "run"
        gold = {row["task_id"]: row for row in self.gold}
        correct = json.dumps({"files": gold[tasks[0]["task_id"]]["after_files"]})
        open_run(run, protocol)
        first = generate_one(tasks[0], protocol, FakeBackend(text=correct))
        write_json(run / "records" / (tasks[0]["task_id"] + ".json"), first)
        second = generate_one(tasks[1], protocol, FakeBackend())
        second["output_text"] += "tampered"
        write_json(run / "records" / (tasks[1]["task_id"] + ".json"), second)
        report = score_run(self.prepared, run)
        overall = report["all_selected_tasks"]
        self.assertEqual(3, overall["denominator"])
        self.assertEqual({"completed": 1, "invalid_receipt": 1, "missing": 1}, overall["status_counts"])
        self.assertEqual(1 / 3, overall["exact_stored_after_file"]["rate"])
        self.assertIsNone(report["functional_success"])
        self.assertIsNone(report["refactoring_success"])
        self.assertFalse(validate_run(run, self.tasks)["complete"])
        destination = self.root / "predictions.jsonl"
        export_predictions(run, destination)
        self.assertEqual(3, len(read_jsonl(destination)))
        self.assertEqual("invalid_receipt", read_jsonl(destination)[1]["status"])
        self.assertIsNone(read_jsonl(destination)[1]["files"])
        self.assertEqual("missing", read_jsonl(destination)[2]["status"])

    def test_saved_receipts_and_diagnostic_reports_are_checked(self):
        protocol, tasks = self.protocol(2)
        run = self.root / "run"
        run_generation(run, protocol, tasks, FakeBackend())
        score_run(self.prepared, run)
        receipt = validate_run(run, self.tasks)
        self.assertTrue(audit_summaries(run, receipt)["complete"])
        saved = read_json(run / "diagnostics.json")
        saved["functional_success"] = 1.0
        write_json(run / "diagnostics.json", saved)
        audit = audit_summaries(run, receipt)
        self.assertFalse(audit["complete"])
        self.assertTrue(audit["aggregate_artifact_errors"])

    def test_revision_and_fixed_protocol_are_enforced(self):
        lock = copy.deepcopy(LOCK)
        lock["models"]["test"]["revision"] = "main"
        with self.assertRaises(ProtocolError):
            make_protocol(self.prepared, "test", lock, CONFIG, "all", 1, 1, .9, False, {})
        config = copy.deepcopy(CONFIG)
        config["max_output_tokens"] = 1024
        with self.assertRaises(ProtocolError):
            make_protocol(self.prepared, "test", LOCK, config, "all", 1, 1, .9, False, {})


if __name__ == "__main__":
    unittest.main()
