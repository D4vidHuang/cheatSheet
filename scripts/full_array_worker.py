#!/usr/bin/env python3
"""Run one frozen full-dataset shard and retain scheduler/attempt evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from swe_refactor.core import (ProtocolError, digest, export_predictions, file_sha,
                               load_prepared, read_json, score_run, select_tasks,
                               source_fingerprint, utc_now, validate_run, write_json)

TERMINAL_JOB_STATES = {
    "BOOT_FAIL", "CANCELLED", "COMPLETED", "DEADLINE", "FAILED", "NODE_FAIL",
    "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "TIMEOUT",
}


def confined(root: Path, name: str) -> Path:
    """Require a canonical relative release path, including resolved symlinks."""
    if not isinstance(name, str) or not name:
        raise ProtocolError("plan path must be a nonempty relative path")
    path = Path(name)
    if path.is_absolute() or path.as_posix() != name or ".." in path.parts:
        raise ProtocolError("unsafe release-relative path: " + repr(name))
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()):
        raise ProtocolError("plan path escapes frozen release")
    return result


def verify_plan(root: Path, plan: dict, model: str, shard_index: int) -> tuple[Path, list[dict]]:
    if plan.get("schema_version") != 1 or plan.get("split") != "all":
        raise ProtocolError("worker requires a schema-1 full-dataset plan")
    models = plan.get("models")
    if not isinstance(models, list) or len(models) != len(set(models)) or model not in models:
        raise ProtocolError("model is absent from or duplicated in the frozen plan")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", model):
        raise ProtocolError("unsafe model alias")
    if "plan_sha256" in plan:
        payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if digest(payload) != plan["plan_sha256"]:
            raise ProtocolError("frozen plan self hash mismatch")
    count = plan.get("num_shards")
    if type(count) is not int or count < 1 or not 0 <= shard_index < count:
        raise ProtocolError("shard coordinates differ from frozen plan")
    prepared = confined(root, plan["prepared"])
    manifest, tasks = load_prepared(prepared)
    hashes = plan.get("hashes", plan)
    actual = {
        "dataset_manifest_sha256": digest(manifest),
        "model_lock_sha256": digest(read_json(root / "configs/models.lock.json")),
        "experiment_sha256": digest(read_json(root / "configs/experiment.json")),
        "harness_source_sha256": source_fingerprint(),
    }
    for name, value in actual.items():
        if hashes.get(name) != value:
            raise ProtocolError("frozen plan hash mismatch: " + name)
    for name, expected in plan.get("source_files_sha256", {}).items():
        if file_sha(confined(root, name)) != expected:
            raise ProtocolError("frozen source hash mismatch: " + name)
    if "task_ids" in plan and plan["task_ids"] != sorted(task["task_id"] for task in tasks):
        raise ProtocolError("frozen plan task coverage differs from prepared tasks")
    selected = select_tasks(tasks, "all", None, count, shard_index)
    return prepared, selected


def scheduler_job_terminal(job_id: str) -> bool:
    """Only the exact parent allocation row can prove a previous owner stopped."""
    if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", job_id):
        return False
    try:
        result = subprocess.run(
            ["sacct", "-n", "-P", "-j", job_id, "--format=JobIDRaw,State"],
            text=True, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if len(fields) >= 2 and fields[0] == job_id:
            state = fields[1].split()[0].rstrip("+") if fields[1].split() else ""
            return state in TERMINAL_JOB_STATES
    return False


def owner_stopped(owner: dict, current: dict) -> bool:
    previous_job = owner.get("slurm_job_id")
    if not isinstance(previous_job, str) or not previous_job:
        return False
    if previous_job == current["slurm_job_id"]:
        previous_restart = owner.get("slurm_restart_count")
        return (type(previous_restart) is int and
                current["slurm_restart_count"] > previous_restart)
    return scheduler_job_terminal(previous_job)


def acquire_worker_lock(shard: Path, current: dict, attempt: Path) -> Path:
    """Recover abandoned locks only after explicit scheduler evidence."""
    guard = shard / "worker.lock"
    if guard.exists():
        previous = read_json(guard)
        if not owner_stopped(previous, current):
            raise ProtocolError("previous worker lock is active or unverified; refusing recovery")
        write_json(attempt / "recovered-worker-lock.json", previous)
        guard.unlink()
    try:
        fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ProtocolError("another worker acquired this shard") from exc
    with os.fdopen(fd, "w") as handle:
        json.dump(current, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return guard


def recover_core_lock(run: Path, current: dict, attempt: Path) -> None:
    lock = run / "active.lock"
    if not lock.exists():
        return
    owner_path = run.parent / "model-owner.json"
    if not owner_path.exists() or not owner_stopped(read_json(owner_path), current):
        raise ProtocolError("generation active.lock owner is unverified; refusing recovery")
    write_json(attempt / "recovered-generation-lock.json", {
        "owner": read_json(owner_path), "lock_text": lock.read_text(),
        "verified_at_utc": utc_now(),
    })
    lock.unlink()


def capture(argv: list[str], destination: Path, timeout: int = 45) -> int:
    try:
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
        destination.write_text(result.stdout, encoding="utf-8")
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        destination.write_text(str(exc) + "\n", encoding="utf-8")
        return 124 if isinstance(exc, subprocess.TimeoutExpired) else 127


def run_child(argv: list[str], log: Path, interrupted: dict) -> int:
    """Forward the batch warning to the entire generation process group."""
    with log.open("w", encoding="utf-8") as output:
        child = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        interrupted["child_pid"] = child.pid
        term_at = None
        while child.poll() is None:
            if interrupted["signal"] is not None:
                if term_at is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    term_at = time.monotonic()
                elif time.monotonic() - term_at >= 30:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(.2)
        return child.returncode


def diagnostics(prepared: Path, run: Path, attempt: Path) -> dict:
    result = {"receipt_complete": False, "all_generation_error": False}
    if not (run / "protocol.json").is_file():
        result["error"] = "generation did not create a protocol"
        return result
    for name, operation in (
            ("validation", lambda: validate_run(run, load_prepared(prepared)[1])),
            ("score", lambda: score_run(prepared, run)),
            ("export", lambda: export_predictions(run, attempt / "predictions.jsonl"))):
        try:
            value = operation()
            write_json(attempt / (name + ".json"), value)
            if name == "validation":
                result["receipt_complete"] = value["complete"]
                counts = value["status_counts"]
                attempted = value["expected_tasks"] - counts.get("out_of_context", 0)
                result["all_generation_error"] = bool(
                    attempted and counts.get("generation_error", 0) == attempted)
                result["status_counts"] = counts
        except (OSError, ProtocolError, ValueError, KeyError, TypeError) as exc:
            result[name + "_error"] = str(exc)
    return result


def execute(root: Path, plan_path: Path, model: str, shard_index: int) -> int:
    plan = read_json(plan_path)
    prepared, selected = verify_plan(root, plan, model, shard_index)
    job_id = os.environ.get("SLURM_JOB_ID", "")
    if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", job_id):
        raise ProtocolError("worker requires a real SLURM_JOB_ID")
    restart_count = int(os.environ.get("SLURM_RESTART_COUNT", "0"))
    if restart_count < 0:
        raise ProtocolError("invalid SLURM_RESTART_COUNT")
    shard = confined(root, plan["run_root"]) / model / f"shard-{shard_index:05d}"
    shard.mkdir(parents=True, exist_ok=True)
    # Re-entry without an explicit scheduler restart is an error, not an overwrite.
    attempt = shard / "attempts" / f"{job_id}-restart-{restart_count}"
    attempt.mkdir(parents=True, exist_ok=False)
    run = shard / "model"
    current = {
        "slurm_job_id": job_id, "slurm_array_job_id": os.environ.get("SLURM_ARRAY_JOB_ID"),
        "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
        "slurm_restart_count": restart_count, "hostname": socket.getfqdn(),
        "worker_pid": os.getpid(), "started_at_utc": utc_now(),
        "plan_sha256": digest(plan), "model": model, "shard_index": shard_index,
        "selected_tasks": len(selected), "selected_task_ids_sha256": digest(
            [task["task_id"] for task in selected]),
    }
    write_json(attempt / "attempt.json", current)
    guard = acquire_worker_lock(shard, current, attempt)
    interrupted = {"signal": None, "child_pid": None}
    previous_handlers = {}
    def stop(signum, _frame):
        interrupted["signal"] = signum
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, stop)
    result = {"exit_code": 1, "generation_exit_code": None}
    try:
        recover_core_lock(run, current, attempt)
        write_json(shard / "model-owner.json", current)
        capture([sys.executable, "-m", "pip", "freeze"], attempt / "environment.txt")
        gpu_returncode = capture(["nvidia-smi"], attempt / "gpu-start.txt")
        result["gpu_start_returncode"] = gpu_returncode
        if gpu_returncode:
            raise ProtocolError("nvidia-smi failed inside allocated GPU shard")
        argv = [sys.executable, "-m", "swe_refactor", "generate",
                "--prepared", str(prepared), "--model", model,
                "--model-lock", str(root / "configs/models.lock.json"),
                "--experiment", str(root / "configs/experiment.json"),
                "--split", "all", "--num-shards", str(plan["num_shards"]),
                "--shard-index", str(shard_index), "--run", str(run),
                "--resume-equivalent-hardware"]
        write_json(attempt / "generation-command.json", argv)
        result["generation_exit_code"] = run_child(argv, attempt / "runner.log", interrupted)
    except (OSError, ProtocolError, ValueError, KeyError, TypeError) as exc:
        result["worker_error"] = str(exc)
    finally:
        result.update(diagnostics(prepared, run, attempt))
        result["signal"] = interrupted["signal"]
        result["finished_at_utc"] = utc_now()
        capture(["nvidia-smi"], attempt / "gpu-final.txt")
        if (result["generation_exit_code"] == 0 and result["receipt_complete"] and
                not result["all_generation_error"] and interrupted["signal"] is None and
                not any(key.endswith("_error") and value for key, value in result.items())):
            result["exit_code"] = 0
        elif interrupted["signal"] is not None:
            result["exit_code"] = 128 + interrupted["signal"]
        write_json(attempt / "result.json", result)
        (attempt / "exit_code.txt").write_text(str(result["exit_code"]) + "\n")
        guard.unlink(missing_ok=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    print(json.dumps({"attempt": str(attempt), **result}, ensure_ascii=False))
    return result["exit_code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--shard-index", required=True, type=int)
    args = parser.parse_args(argv)
    root = (args.root or args.plan.parent.parent).resolve()
    try:
        return execute(root, args.plan.resolve(), args.model, args.shard_index)
    except (OSError, ProtocolError, ValueError, KeyError, TypeError) as exc:
        print("worker error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
