"""Tests for evaluation gates, not evidence of Java dataset performance."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "repository_evaluator", Path(__file__).resolve().parents[1] / "scripts/evaluate_repositories.py")
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


class RepositoryEvaluatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_patch_rejects_escape_symlink_duplicate_and_non_java(self):
        (self.root / "src").mkdir()
        (self.root / "link").symlink_to(self.root / "src", target_is_directory=True)
        for name in ("../A.java", "/A.java", "a/.git/A.java", "pom.xml", "link/A.java"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                evaluator.apply_files(self.root, [{"path": name, "content": "x"}])
        with self.assertRaises(ValueError):
            evaluator.apply_files(self.root, [{"path": "A.java", "content": "a"},
                                              {"path": "A.java", "content": "b"}])
        self.assertFalse((self.root / "A.java").exists())

    def test_compound_and_source_location_are_required(self):
        raw = {"type": "Move And Rename Method", "filePathBefore": "A.java",
               "filePathAfter": "B.java", "description": "to class pkg.B"}
        ref = {"type": "Move Method", "description": "to class pkg.B",
               "leftSideLocations": [{"filePath": "A.java", "startLine": 4, "endLine": 9,
                                       "codeElementType": "METHOD_DECLARATION"}],
               "rightSideLocations": [{"filePath": "B.java"}]}
        self.assertFalse(evaluator.matches_target(ref, raw, (4, 9)))
        ref["type"] = "Move And Rename Method"
        self.assertTrue(evaluator.matches_target(ref, raw, (4, 9)))
        self.assertFalse(evaluator.matches_target(ref, raw, (20, 30)))
        ref["rightSideLocations"] = [{"filePath": "C.java"}]
        self.assertFalse(evaluator.matches_target(ref, raw, (4, 9)))

    def test_miner_json_must_have_requested_commit(self):
        path = self.root / "miner.json"
        path.write_text(json.dumps({"commits": [{"sha1": "wrong", "refactorings": []}]}))
        with self.assertRaises(ValueError):
            evaluator.parse_refactorings(path, "wanted")
        path.write_text(json.dumps({"commits": [{"sha1": "wanted", "refactorings": []}]}))
        self.assertEqual(evaluator.parse_refactorings(path, "wanted"), [])

    def test_wrong_helper_does_not_match_private_gold_target(self):
        gold = {"right_methods": [["A.java", "private requested() : int"]], "classes": ["pkg.A"]}
        wrong = {"rightSideLocations": [{"filePath": "A.java", "codeElementType": "METHOD_DECLARATION",
                                        "codeElement": "private unrelated() : int"}],
                 "description": "Extract Method in class pkg.A"}
        self.assertFalse(evaluator.matches_gold_target(wrong, gold))
        wrong["rightSideLocations"][0]["codeElement"] = "private requested() : int"
        self.assertTrue(evaluator.matches_gold_target(wrong, gold))
        wrong["description"] = "Extract Method in class pkg.B"
        self.assertFalse(evaluator.matches_gold_target(wrong, gold))

    def test_export_invalid_receipt_placeholder_retains_task_and_rejects_files(self):
        task = {"task_id": "selected", "task_sha256": "task-hash"}
        protocol = {"model_key": "qwen35-2b"}
        row = {"task_id": "selected", "status": "invalid_receipt", "files": None,
               "error": "Corrupt original record hash"}
        evaluator.validate_protocol_predictions([task], {"selected": row}, protocol)
        self.assertEqual(row["status"], "invalid_receipt")
        row["files"] = [{"path": "A.java", "content": "untrusted"}]
        with self.assertRaises(ValueError):
            evaluator.validate_protocol_predictions([task], {"selected": row}, protocol)

    def test_completed_receipt_duplicate_json_keys_rejected_even_when_hashed(self):
        import hashlib
        task = {"task_id": "selected", "task_sha256": "task-hash"}
        protocol = {"model_key": "qwen35-2b"}
        files = [{"path": "A.java", "content": "class A {}"}]
        raw = '{"files":[],"files":' + json.dumps(files) + '}'
        row = {"task_id": "selected", "task_sha256": "task-hash", "status": "completed",
               "model_key": "qwen35-2b", "protocol_sha256": evaluator.digest(protocol),
               "output_text": raw, "output_sha256": hashlib.sha256(raw.encode()).hexdigest(),
               "files": files}
        row["record_sha256"] = evaluator.digest(row)
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            evaluator.validate_protocol_predictions([task], {"selected": row}, protocol)

    def test_candidate_tool_errors_are_unscored_after_valid_controls(self):
        before = "class A { void m() {} }"
        after = "class A { void m() { helper(); } void helper() {} }"
        task = {"task_id": "task", "project": "toy", "commit_id": "gold-commit",
                "input": {"files": [{"path": "A.java", "content": before}]}}
        raw = {"type": "Extract Method", "filePathBefore": "A.java", "filePathAfter": "A.java",
               "description": "extract helper", "sourceCodeBeforeRefactoring": "void m() {}"}
        files = [{"path": "A.java", "content": after}]
        fp = {"right_methods": [["A.java", "helper() : void"]], "classes": ["A"]}
        passed = {"compile": {"status": "passed"}, "tests": {"status": "passed"}}
        noop = {"status": "passed", "target_detected": False, "detected_types": []}
        detected = {"status": "passed", "target_detected": True, "target_fingerprints": [fp]}
        cases = [
            ("miner_tool_error", {"status": "tool_error", "target_detected": None}, passed, None),
            ("miner_timeout", {"status": "timeout", "target_detected": None}, passed, None),
            ("compile_timeout", detected,
             {"compile": {"status": "timeout"}, "tests": {"status": "not_run"}}, None),
            ("test_tool_error", detected,
             {"compile": {"status": "passed"}, "tests": {"status": "tool_error"}}, None),
            ("real_compile_failure", detected,
             {"compile": {"status": "failed"}, "tests": {"status": "not_run"}}, False),
            ("real_test_failure", detected,
             {"compile": {"status": "passed"}, "tests": {"status": "failed"}}, False),
        ]
        def fake_clone(source, destination, base, logs):
            destination.mkdir()
            (destination / "A.java").write_text(before)
        def fake_git(root, *args):
            return " M A.java" if args[0] == "status" else "base"
        for name, miner_result, build_result, strict in cases:
            with self.subTest(name=name):
                workspace = self.root / name
                workspace.mkdir()
                with patch.object(evaluator, "clone_at", side_effect=fake_clone), \
                     patch.object(evaluator, "git", side_effect=fake_git), \
                     patch.object(evaluator, "commit_candidate", return_value="candidate"), \
                     patch.object(evaluator, "run_build", side_effect=[passed, passed, build_result]), \
                     patch.object(evaluator, "run_miner", side_effect=[noop, detected, miner_result]):
                    result = evaluator.evaluate_task(task, raw, {"status": "completed", "files": files},
                        {"repository": str(self.root)}, self.root / "miner", workspace,
                        self.root / (name + "-logs"),
                        {"base_commit": "base", "provenance": "synthetic test fixture", "files": files})
                self.assertTrue(result["eligible"])
                self.assertIs(result["strict_success"], strict)
                self.assertEqual(result["status"], "candidate_evaluation_error" if strict is None else "evaluated")
                if strict is None:
                    self.assertTrue(result["candidate_evaluation_errors"])
    def test_skipped_or_stale_tests_do_not_pass(self):
        report = self.root / "results.xml"
        report.write_text('<testsuite tests="4" skipped="0" failures="0" errors="0"/>')
        config = {"compile_command": [sys.executable, "-c", "pass"],
                  "test_command": [sys.executable, "-c", "pass"],
                  "test_report_globs": ["results.xml"]}
        result = evaluator.run_build(self.root, config, self.root / "logs")
        self.assertEqual(result["tests"]["status"], "no_passing_test_receipt")
        config["test_command"] = [sys.executable, "-c",
            "from pathlib import Path; Path('results.xml').write_text('<testsuite tests=\"4\" skipped=\"4\"/>')"]
        result = evaluator.run_build(self.root, config, self.root / "logs2")
        self.assertEqual(result["tests"]["status"], "no_passing_test_receipt")

    def test_summary_separates_unscored_eligible_from_real_failures(self):
        rows = [{"eligible": True, "strict_success": True, "status": "evaluated"},
                {"eligible": True, "strict_success": False, "status": "evaluated"},
                {"eligible": True, "strict_success": None, "status": "candidate_evaluation_error"},
                {"eligible": False, "strict_success": None, "status": "baseline_failed"}]
        summary = evaluator.summarize_results(rows)
        self.assertEqual(summary["requested"], 4)
        self.assertEqual(summary["eligible"], 3)
        self.assertEqual(summary["unscored_eligible"], 1)
        self.assertEqual(summary["scored_eligible"], 2)
        self.assertEqual(summary["strict_success_rate_scored_eligible"], .5)
        self.assertEqual(summary["verified_success_fraction_all_requested"], .25)

    def test_noop_and_missing_gold_gate_with_isolated_real_git(self):
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        before = "class A {\n  void m() { System.out.println(1); }\n}\n"
        (source / "A.java").write_text(before)
        evaluator.commit_candidate(source, [{"path": "A.java", "content": before}])
        base = evaluator.git(source, "rev-parse", "HEAD")
        evaluator.commit_candidate(source)
        gold_commit = evaluator.git(source, "rev-parse", "HEAD")
        task = {"task_id": "task", "project": "toy", "commit_id": gold_commit,
                "input": {"files": [{"path": "A.java", "content": before}]}}
        raw = {"type": "Extract Method", "filePathBefore": "A.java",
               "filePathAfter": "A.java", "description": "extract helper",
               "sourceCodeBeforeRefactoring": "void m() { System.out.println(1); }"}
        # Fake miner used only to test runner control flow. This is explicitly
        # not a test of RefactoringMiner's detection accuracy.
        fake = self.root / "miner.sh"
        fake.write_text("#!/bin/sh\n" + sys.executable + " -c 'import json,sys; "
                        "json.dump({\"commits\":[{\"sha1\":sys.argv[1],\"refactorings\":[]}]},"
                        "open(sys.argv[2],\"w\"))' \"$3\" \"$5\"\n")
        test_code = "from pathlib import Path; Path('results.xml').write_text('<testsuite tests=\"1\" failures=\"0\"/>')"
        config = {"repository": str(source), "compile_command": [sys.executable, "-c", "pass"],
                  "test_command": [sys.executable, "-c", test_code], "test_report_globs": ["results.xml"]}
        work = self.root / "work"
        work.mkdir()
        prediction = {"status": "completed", "files": [{"path": "A.java", "content": before}]}
        result = evaluator.evaluate_task(task, raw, prediction, config, fake, work, self.root / "logs")
        self.assertEqual(result["base_commit"], base)
        self.assertEqual(result["status"], "no_change")
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["strict_success"])
        self.assertFalse(result["noop_control"]["target_detected"])
        self.assertEqual((source / "A.java").read_text(), before)
        self.assertEqual(evaluator.git(source, "rev-parse", "HEAD"), gold_commit)
        invalid_work = self.root / "invalid-work"
        invalid_work.mkdir()
        invalid = {"task_id": "task", "status": "invalid_receipt", "files": None,
                   "error": "Sanitized corrupt source receipt"}
        result = evaluator.evaluate_task(task, raw, invalid, config, fake, invalid_work,
                                        self.root / "invalid-logs")
        self.assertEqual(result["status"], "missing_or_invalid_generation")
        self.assertEqual(result["generation_status"], "invalid_receipt")


if __name__ == "__main__":
    unittest.main()
