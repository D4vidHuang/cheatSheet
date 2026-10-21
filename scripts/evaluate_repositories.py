#!/usr/bin/env python3
"""Independent, conservative repository evaluation; never trust upstream result flags.

This runner operates only in new temporary clones. Project commands are trusted
configuration, never taken from generated output or the dataset's compileCommand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def validate_protocol_predictions(tasks, predictions, protocol):
    if any(row.get("model_key") not in (None, protocol["model_key"])
           for row in predictions.values()):
        raise ValueError("Predictions contain another model")
    for task in tasks:
        row = predictions.get(task["task_id"])
        if row is None or row.get("status") in {"missing", "invalid_receipt"}:
            # Export sanitizes corrupt receipts instead of passing untrusted
            # files onward. Keep these rows in the selected task denominator.
            if row and row.get("files") is not None:
                raise ValueError("Missing/invalid receipt placeholder cannot contain files")
            continue
        body = {k: v for k, v in row.items() if k != "record_sha256"}
        raw = row.get("output_text")
        if (not isinstance(raw, str) or row.get("record_sha256") != digest(body)
                or row.get("task_sha256") != task["task_sha256"]
                or row.get("protocol_sha256") != digest(protocol)
                or row.get("output_sha256") != hashlib.sha256(raw.encode()).hexdigest()):
            raise ValueError("Prediction receipt hash/provenance mismatch")
        if row.get("status") == "completed":
            parsed = json.loads(raw, object_pairs_hook=no_duplicate_keys)
            if parsed != {"files": row.get("files")}:
                raise ValueError("Prediction files differ from raw output")
        elif row.get("files") is not None:
            raise ValueError("Failed generation cannot contain scored files")


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{number}: expected object")
                rows.append(row)
    ids = [r["task_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate task_id in {path}")
    return rows


def safe_path(root, value):
    """Permit repository-relative Java files; reject traversal and symlinks."""
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Invalid file path")
    name = PurePosixPath(value)
    if name.is_absolute() or not name.parts or any(p in ("..", ".git") for p in name.parts):
        raise ValueError("Unsafe file path")
    if name.suffix != ".java":
        raise ValueError("Only .java source changes are permitted")
    target = root.joinpath(*name.parts)
    for parent in [target, *target.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("Symlink source path")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("File escapes repository")
    return target


def apply_files(root, files):
    if not isinstance(files, list) or not files:
        raise ValueError("Missing files list")
    seen = set()
    validated = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
            raise ValueError("Each file must contain exactly path and content")
        target = safe_path(root, entry["path"])
        if entry["path"] in seen:
            raise ValueError("Duplicate output file")
        seen.add(entry["path"])
        value = entry["content"]
        if value is not None and not isinstance(value, str):
            raise ValueError("File content must be string or null")
        if value is None and not target.is_file():
            raise ValueError("Cannot delete missing file")
        validated.append((target, value))
    for target, value in validated:
        if value is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")


def command(argv, cwd, log_path, timeout=1800, env=None):
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("Commands must be nonempty argv lists")
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as stream:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return {"status": "timeout", "returncode": None,
                        "seconds": time.monotonic() - started, "log": str(log_path)}
        return {"status": "passed" if code == 0 else "failed", "returncode": code,
                "seconds": time.monotonic() - started, "log": str(log_path)}
    except OSError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return {"status": "tool_error", "returncode": None,
                "seconds": time.monotonic() - started, "log": str(log_path)}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def clone_at(source, destination, base, logs):
    result = command(["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-hardlinks",
                      "--no-checkout", str(source), str(destination)], source,
                     logs / "clone.log", 300)
    if result["status"] != "passed":
        raise RuntimeError(f"Repository clone failed; see {result['log']}")
    git(destination, "config", "core.hooksPath", "/dev/null")
    git(destination, "config", "commit.gpgsign", "false")
    # Preserve the actual upstream URL for RefactoringMiner JSON receipts.
    try:
        origin = git(source, "remote", "get-url", "origin")
        git(destination, "remote", "set-url", "origin", origin)
    except subprocess.CalledProcessError:
        pass
    git(destination, "checkout", "--detach", base)


def commit_candidate(root, files=None):
    if files:
        git(root, "add", "-f", "-A", "--", *[entry["path"] for entry in files])
    git(root, "-c", "user.name=SWE-Refactor evaluator", "-c",
        "user.email=evaluator@example.invalid", "commit", "--allow-empty", "--no-verify",
        "-m", "Isolated evaluation candidate")
    return git(root, "rev-parse", "HEAD")


def normalize_type(value):
    return " ".join(value.casefold().split())


def parse_refactorings(path, expected_commit):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("commits"), list):
        raise ValueError("Unknown RefactoringMiner JSON schema")
    commits = payload["commits"]
    if len(commits) != 1 or commits[0].get("sha1") != expected_commit:
        raise ValueError("RefactoringMiner did not evaluate the requested commit")
    refs = commits[0].get("refactorings")
    if not isinstance(refs, list) or not all(isinstance(r, dict) and isinstance(r.get("type"), str)
                                             for r in refs):
        raise ValueError("Invalid refactorings array")
    return refs


def source_span(raw, before_content):
    snippet = raw.get("sourceCodeBeforeRefactoring", "")
    if not snippet or before_content.count(snippet) != 1:
        raise ValueError("Source method/snippet cannot be uniquely located in baseline")
    offset = before_content.index(snippet)
    start = before_content.count("\n", 0, offset) + 1
    return start, start + snippet.count("\n")


def matches_target(ref, raw, span):
    # Exact compound type is required; plain Move does not satisfy Move And Rename.
    if normalize_type(ref["type"]) != normalize_type(raw["type"]):
        return False
    left = ref.get("leftSideLocations", [])
    if not any(loc.get("filePath") == raw["filePathBefore"]
               and isinstance(loc.get("startLine"), int)
               and isinstance(loc.get("endLine"), int)
               and loc["startLine"] <= span[1] and loc["endLine"] >= span[0]
               and "METHOD" in loc.get("codeElementType", "")
               for loc in left):
        return False
    # Require a recorded destination for moves; nested classes can share a file.
    if "move" in normalize_type(raw["type"]):
        target_file = raw["filePathAfter"]
        if target_file != raw["filePathBefore"]:
            if not any(loc.get("filePath") == target_file
                       for loc in ref.get("rightSideLocations", [])):
                return False
        target = re.search(r"(?:moved to class|to class)\s+([\w.$]+)", raw["description"])
        if target and target.group(1) not in ref.get("description", ""):
            return False
    return True


def target_fingerprint(ref):
    """Conservative guided-goal endpoints derived only by the private evaluator."""
    endpoints = {(loc.get("filePath"), loc.get("codeElement"))
                 for loc in ref.get("rightSideLocations", [])
                 if loc.get("codeElementType") == "METHOD_DECLARATION" and loc.get("codeElement")}
    classes = set(re.findall(r"(?:in|from|to) class\s+([\w.$]+)", ref.get("description", "")))
    return {"right_methods": sorted([list(x) for x in endpoints]), "classes": sorted(classes)}


def matches_gold_target(candidate, expected):
    actual = target_fingerprint(candidate)
    return (bool(expected["right_methods"])
            and set(map(tuple, expected["right_methods"])).issubset(map(tuple, actual["right_methods"]))
            and set(expected["classes"]).issubset(actual["classes"]))


def run_miner(root, miner, raw, span, logs, timeout):
    head = git(root, "rev-parse", "HEAD")
    output = logs / "refactorings.json"
    if output.exists():
        raise ValueError("RefactoringMiner output path already exists")
    result = command(["sh", str(miner), "-c", str(root), head, "-json", str(output)],
                     root, logs / "miner.log", timeout)
    if result["status"] != "passed":
        return {**result, "target_detected": None}
    try:
        refs = parse_refactorings(output, head)
        return {**result, "target_detected": any(matches_target(r, raw, span) for r in refs),
                "target_fingerprints": [target_fingerprint(r) for r in refs if matches_target(r, raw, span)],
                "detected_types": [r["type"] for r in refs], "json": str(output)}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {**result, "status": "tool_error", "target_detected": None, "error": str(exc)}


def run_build(root, config, logs):
    env = os.environ.copy()
    if config.get("java_home"):
        env["JAVA_HOME"] = config["java_home"]
        env["PATH"] = str(Path(config["java_home"]) / "bin") + os.pathsep + env["PATH"]
    timeout = config.get("timeout_seconds", 1800)
    compile_result = command(config["compile_command"], root, logs / "compile.log", timeout, env)
    # Delete old receipts, including any produced by the compile step, so the
    # test command must produce its own result in this disposable checkout.
    for pattern in config["test_report_globs"]:
        for report in root.glob(pattern):
            if report.is_file() or report.is_symlink():
                report.unlink()
    test_result = ({"status": "not_run", "returncode": None} if compile_result["status"] != "passed"
                   else command(config["test_command"], root, logs / "test.log", timeout, env))
    # A zero exit status alone cannot establish that tests executed. A configured
    # receipt must be produced freshly, e.g. Maven Surefire / Gradle JUnit XML.
    reports = []
    if test_result["status"] == "passed":
        import xml.etree.ElementTree as ET
        for pattern in config["test_report_globs"]:
            reports.extend(root.glob(pattern))
        tests = failures = errors = skipped = 0
        try:
            for report in set(reports):
                xml = ET.parse(report).getroot()
                suites = [xml] if xml.tag == "testsuite" else list(xml.iter("testsuite"))
                for suite in suites:
                    tests += int(suite.attrib.get("tests", 0))
                    failures += int(suite.attrib.get("failures", 0))
                    errors += int(suite.attrib.get("errors", 0))
                    skipped += int(suite.attrib.get("skipped", 0))
            test_result.update(tests=tests, failures=failures, errors=errors, skipped=skipped,
                               report_count=len(set(reports)))
            if tests <= skipped or failures or errors:
                test_result["status"] = "no_passing_test_receipt" if tests <= skipped else "failed"
        except (ET.ParseError, OSError, ValueError) as exc:
            test_result.update(status="tool_error", error=str(exc))
    return {"compile": compile_result, "tests": test_result}


def checks_pass(result):
    return (result["compile"]["status"] == "passed" and result["tests"]["status"] == "passed")


def candidate_evaluation_errors(miner, build):
    errors = []
    if miner["status"] != "passed":
        errors.append({"stage": "refactoring_miner", "status": miner["status"]})
    for stage in ("compile", "tests"):
        if build[stage]["status"] in {"timeout", "tool_error"}:
            errors.append({"stage": stage, "status": build[stage]["status"]})
    return errors


def summarize_results(results):
    eligible = [row for row in results if row["eligible"]]
    scored = [row for row in eligible if row["strict_success"] is not None]
    successes = sum(row["strict_success"] is True for row in scored)
    return {"requested": len(results), "eligible": len(eligible),
            "scored_eligible": len(scored), "unscored_eligible": len(eligible) - len(scored),
            "strict_successes": successes,
            "verified_success_fraction_all_requested": successes / len(results) if results else None,
            "strict_success_rate_scored_eligible": successes / len(scored) if scored else None,
            "statuses": {status: sum(row["status"] == status for row in results)
                         for status in sorted({row["status"] for row in results})},
            "scope": "Operational evidence: target RM detection plus configured compilation and regression tests; not a proof of semantic equivalence."}


def evaluate_task(task, raw, prediction, config, miner, workspace, logs, gold_patch=None):
    logs.mkdir(parents=True, exist_ok=False)
    result = {"task_id": task["task_id"], "project": task["project"],
              "commit_id": task["commit_id"], "strict_success": None,
              "eligible": False, "status": "infrastructure_error"}
    source = Path(config["repository"]).expanduser().resolve()
    base = git(source, "rev-parse", task["commit_id"] + "^1")
    result["base_commit"] = base
    baseline = workspace / "baseline"
    clone_at(source, baseline, base, logs / "baseline")
    for entry in task["input"]["files"]:
        target = safe_path(baseline, entry["path"])
        if not target.is_file() or target.read_text(encoding="utf-8") != entry["content"]:
            result.update(status="base_source_mismatch", mismatch_path=entry["path"])
            return result
    before = safe_path(baseline, raw["filePathBefore"]).read_text(encoding="utf-8")
    try:
        span = source_span(raw, before)
    except ValueError as exc:
        result.update(status="source_not_uniquely_locatable", error=str(exc))
        return result
    result["baseline"] = run_build(baseline, config, logs / "baseline")
    if not checks_pass(result["baseline"]):
        result["status"] = "baseline_failed"
        return result
    commit_candidate(baseline)  # Empty commit must not be detected as requested refactoring.
    result["noop_control"] = run_miner(baseline, miner, raw, span, logs / "noop", config.get("miner_timeout_seconds", 300))
    if (result["noop_control"].get("target_detected") is not False
            or result["noop_control"].get("detected_types")):
        result["status"] = "noop_control_failed"
        return result
    result["gold_control"] = {"status": "missing_complete_gold_patch"}
    if gold_patch:
        if gold_patch.get("base_commit") != base or not gold_patch.get("provenance"):
            raise ValueError("Gold patch needs exact base_commit and nonempty provenance")
        gold = workspace / "gold"
        clone_at(source, gold, base, logs / "gold")
        apply_files(gold, gold_patch["files"])
        commit_candidate(gold, gold_patch["files"])
        detection = run_miner(gold, miner, raw, span, logs / "gold", config.get("miner_timeout_seconds", 300))
        build = run_build(gold, config, logs / "gold")
        result["gold_control"] = {"miner": detection, **build}
        result["eligible"] = (detection.get("target_detected") is True and checks_pass(build)
                              and any(fp["right_methods"] for fp in detection.get("target_fingerprints", [])))
        if not result["eligible"]:
            result["status"] = "gold_control_failed"
            return result
    if prediction is None or prediction.get("status") != "completed" or not prediction.get("files"):
        result.update(status="missing_or_invalid_generation",
                      generation_status=prediction.get("status") if prediction else "missing",
                      strict_success=False if result["eligible"] else None)
        return result
    candidate = workspace / "candidate"
    clone_at(source, candidate, base, logs / "candidate")
    try:
        requested_test_paths = {raw["filePathBefore"], raw["filePathAfter"]}
        for entry in prediction["files"]:
            name = entry.get("path", "")
            if (re.search(r"(?:^|/)(?:test|tests|testFixtures)(?:/|$)", name)
                    and name not in requested_test_paths):
                raise ValueError("Candidate changes an unrelated test file")
        apply_files(candidate, prediction["files"])
    except ValueError as exc:
        result.update(status="invalid_patch", error=str(exc),
                      strict_success=False if result["eligible"] else None)
        return result
    if not git(candidate, "status", "--porcelain", "--untracked-files=all"):
        result.update(status="no_change", strict_success=False if result["eligible"] else None)
        return result
    result["candidate_commit"] = commit_candidate(candidate, prediction["files"])
    result["candidate_miner"] = run_miner(candidate, miner, raw, span, logs / "candidate", config.get("miner_timeout_seconds", 300))
    result["candidate"] = run_build(candidate, config, logs / "candidate")
    errors = candidate_evaluation_errors(result["candidate_miner"], result["candidate"])
    if errors:
        result.update(status="candidate_evaluation_error", candidate_checks_pass=None,
                      candidate_evaluation_errors=errors, strict_success=None)
        return result
    observed = result["candidate_miner"].get("target_detected") is True and checks_pass(result["candidate"])
    result["guided_goal_matches_gold"] = None
    if result["eligible"]:
        # Type and source overlap alone could accept extraction of the wrong helper.
        # Compare private gold target declaration signatures and class identities.
        expected = result["gold_control"]["miner"]["target_fingerprints"]
        actual = result["candidate_miner"].get("target_fingerprints", [])
        result["guided_goal_matches_gold"] = any(
            bool(g["right_methods"]) and set(map(tuple, g["right_methods"])).issubset(map(tuple, a["right_methods"]))
            and set(g["classes"]).issubset(a["classes"]) for g in expected for a in actual)
        observed = observed and result["guided_goal_matches_gold"]
    result.update(status="evaluated" if result["eligible"] else "evaluated_gold_pending",
                  candidate_checks_pass=observed,
                  strict_success=observed if result["eligible"] else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--protocol", type=Path, help="Select and hash-check exactly this generation run's task IDs")
    parser.add_argument("--repositories", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("data/SWE-Refactor/pure_refactoring_data.json"))
    parser.add_argument("--refactoring-miner", required=True, type=Path)
    parser.add_argument("--gold-patches", type=Path, help="Manually verified complete patches; never raw stored after_files")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    tasks = read_jsonl(args.tasks)
    if args.protocol:
        protocol = json.loads(args.protocol.read_text())
        selected = protocol["selected_task_ids"]
        by_id = {task["task_id"]: task for task in tasks}
        if not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
            parser.error("Invalid protocol selection")
        if any(task_id not in by_id for task_id in selected):
            parser.error("Protocol selection contains unknown tasks")
        tasks = [by_id[task_id] for task_id in selected]
        if any(task["task_sha256"] != protocol["selected_task_sha256"][task["task_id"]]
               or digest({k: v for k, v in task.items() if k != "task_sha256"}) != task["task_sha256"]
               for task in tasks):
            parser.error("Task hashes differ from generation protocol")
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        tasks = tasks[:args.limit]
    predictions = {r["task_id"]: r for r in read_jsonl(args.predictions)}
    if args.protocol:
        try:
            validate_protocol_predictions(tasks, predictions, protocol)
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
    gold = {r["task_id"]: r for r in read_jsonl(args.gold_patches)} if args.gold_patches else {}
    raw = {(r["projectName"], r["uniqueId"]): r for r in json.loads(args.dataset.read_text())}
    configs = json.loads(args.repositories.read_text())
    miner = args.refactoring_miner.resolve()
    if not miner.is_file():
        parser.error("RefactoringMiner launcher does not exist")
    if args.output.exists():
        parser.error("Output exists; choose a new run directory")
    args.output.mkdir(parents=True)
    if args.work_root:
        args.work_root.mkdir(parents=True, exist_ok=True)
    results = []
    for task in tasks:
        task_id = task["task_id"]
        folder = hashlib.sha256(task_id.encode()).hexdigest()[:24]
        try:
            config = configs[task["project"]]
            for key in ("repository", "compile_command", "test_command", "test_report_globs"):
                if not config.get(key):
                    raise ValueError(f"Missing trusted project configuration: {key}")
            with tempfile.TemporaryDirectory(prefix="swe-eval-", dir=args.work_root) as temp:
                row = evaluate_task(task, raw[(task["project"], task["upstream_unique_id"])],
                                    predictions.get(task_id), config, miner, Path(temp),
                                    args.output.resolve() / "logs" / folder, gold.get(task_id))
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            row = {"task_id": task_id, "eligible": False, "strict_success": None,
                   "status": "infrastructure_error", "error": str(exc)}
        results.append(row)
        with (args.output / "evaluations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"task_id": task_id, "status": row["status"]}), flush=True)
    summary = summarize_results(results)
    summary["input_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in [args.tasks, args.predictions, args.repositories, args.dataset,
                                            args.refactoring_miner,
                                            *([args.protocol] if args.protocol else []),
                                            *([args.gold_patches] if args.gold_patches else [])]}
    summary["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
