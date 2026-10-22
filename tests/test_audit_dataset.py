"""Synthetic research-integrity checks; no private benchmark files required."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "audit_dataset", Path(__file__).resolve().parents[1] / "scripts/audit_dataset.py")
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def task(task_id="t1", **updates):
    row = {
        "projectName": "toy",
        "uniqueId": task_id,
        "type": "Extract Method",
        "commitId": "a" * 40,
        "filePathBefore": "src/main/java/A.java",
        "filePathAfter": "src/main/java/A.java",
        "methodNameBefore": "toy.A#m",
        "description": "Extract Method helper from m in class toy.A",
        "sourceCodeBeforeRefactoring": "int m() { return 42; }",
        "sourceCodeBeforeForWhole": "class A { int m() { return 42; } }",
        "sourceCodeAfterRefactoring": "int helper() { return 42; }",
        "sourceCodeAfterForWhole": "class A { int m() { return helper(); } int helper() { return 42; } }",
        "compileCommand": "mvn test",
        "compileJDK": 17,
        "compileResultBefore": True,
        "compileResultCurrent": True,
        "coverageInfo": {"LINE": {"missed": 0, "covered": 1}},
        "callInfo": "N/A",
    }
    row.update(updates)
    return row


class AuditDatasetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "pure_refactoring_data.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        self.data.write_text(json.dumps(rows), encoding="utf-8")

    def test_tasks_sharing_a_commit_are_not_silently_deduplicated(self):
        self.write([task("one"), task("two", type="Inline Method")])
        report = audit_module.audit(self.data)
        self.assertEqual(report["tasks"], 2)
        self.assertEqual(report["distinct_project_commit_pairs"], 1)
        self.assertEqual(report["duplicates"]["uniqueId"]["duplicate_excess"], 0)
        self.assertEqual(report["duplicates"]["project_commit_before_file_and_source"]["duplicate_excess"], 1)

    def test_test_skip_false_and_broken_commands_are_distinguished(self):
        self.write([
            task("tests-enabled", compileCommand="mvn test -DskipTests=false"),
            task("tests-skipped", compileCommand="mvn package -DskipTests"),
            task("broken-command", compileCommand="mvn test -Dtest='!Excluded"),
        ])
        report = audit_module.audit(self.data)
        self.assertEqual(report["anomalies"]["explicit_test_skip_command_tasks"], 1)
        self.assertEqual(report["anomalies"]["malformed_build_command_tasks"], 1)

    def test_empty_and_repeated_snippets_do_not_claim_safe_replacement(self):
        self.write([
            task("empty", sourceCodeBeforeRefactoring=""),
            task("repeated", sourceCodeBeforeRefactoring="return 42;",
                 sourceCodeBeforeForWhole="class A { int x() { return 42; } int y() { return 42; } }"),
        ])
        hazards = audit_module.audit(self.data)["anomalies"]["replacement_hazards"]
        self.assertEqual([entry["reason"] for entry in hazards],
                         ["empty_before_snippet", "nonunique_before_snippet"])
        self.assertIsNone(hazards[0]["occurrences"])
        self.assertEqual(hazards[1]["occurrences"], 2)

    def test_split_copies_cannot_inflate_canonical_denominator(self):
        one, two = task("one"), task("two", projectName="other")
        self.write([one, two])
        copy_folder = self.root / "code/data/toy"
        copy_folder.mkdir(parents=True)
        old_copy = {key: value for key, value in one.items() if key != "projectName"}
        old_copy["invokedMethod"] = []
        (copy_folder / "toy_pure_refactoring_data.json").write_text(json.dumps([old_copy]))
        report = audit_module.audit(self.data)
        self.assertEqual(report["tasks"], 2)
        self.assertEqual(report["per_project_copies"]["distinct_ids"], 1)
        self.assertEqual(report["per_project_copies"]["canonical_tasks_missing_from_copies"], 1)
        self.assertEqual(report["per_project_copies"]["files"][0]["records_differing_from_canonical"], 1)

    def test_report_is_source_free_deterministic_and_does_not_modify_input(self):
        sentinel = "PRIVATE_JAVA_BODY_SENTINEL"
        self.write([task(sourceCodeBeforeRefactoring=sentinel,
                         sourceCodeBeforeForWhole="class A { " + sentinel + " }",
                         sourceCodeAfterRefactoring=sentinel + "_AFTER",
                         sourceCodeAfterForWhole="class A { " + sentinel + "_AFTER }")])
        before = self.data.read_bytes()
        first = json.dumps(audit_module.audit(self.data), sort_keys=True)
        second = json.dumps(audit_module.audit(self.data), sort_keys=True)
        self.assertEqual(first, second)
        self.assertNotIn(sentinel, first)
        self.assertEqual(self.data.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
