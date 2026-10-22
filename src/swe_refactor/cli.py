from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (ProtocolError, audit_summaries, export_predictions, load_prepared,
                   make_protocol, open_run, prepare, read_json, run_generation,
                   reconcile_resume_protocol, score_run, validate_run)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Auditable SWE-Refactor snapshot-context experiment")
    sub = cli.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="freeze source-only tasks and evaluation-only gold")
    prep.add_argument("--dataset", type=Path, default=Path("data/SWE-Refactor/pure_refactoring_data.json"))
    prep.add_argument("--output", type=Path, default=Path("artifacts/dataset-v1"))
    prep.add_argument("--seed", type=int, default=42)
    prep.add_argument("--dev-ratio", type=float, default=.1)
    prep.add_argument("--pilot-ratio", type=float, default=.1)
    gen = sub.add_parser("generate", help="one greedy completion per task, with resumable receipts")
    gen.add_argument("--prepared", type=Path, default=Path("artifacts/dataset-v1"))
    gen.add_argument("--model-lock", type=Path, default=Path("configs/models.lock.json"))
    gen.add_argument("--experiment", type=Path, default=Path("configs/experiment.json"))
    gen.add_argument("--model", required=True, help="an exact model key from models.lock.json")
    gen.add_argument("--split", choices=["dev", "pilot", "test", "all"], default="pilot")
    gen.add_argument("--limit", type=int, help="explicit smoke subset; changes the run protocol and denominator")
    gen.add_argument("--num-shards", type=int, default=1,
                     help="split the selected split into this many deterministic, disjoint shards")
    gen.add_argument("--shard-index", type=int, default=0,
                     help="zero-based shard index; selected before applying an optional --limit")
    gen.add_argument("--resume-equivalent-hardware", action="store_true",
                     help="allow only changed GPU UUID/device indices; record the new environment attempt")
    gen.add_argument("--run", "--run-dir", required=True, type=Path)
    gen.add_argument("--tensor-parallel", type=int, default=1)
    gen.add_argument("--gpu-memory-utilization", type=float, default=.9)
    gen.add_argument("--tokenize-only", action="store_true", help="no generation; cannot be resumed as a generation run")
    for command in ("score", "validate"):
        child = sub.add_parser(command)
        child.add_argument("--prepared", type=Path, default=Path("artifacts/dataset-v1"))
        child.add_argument("--run", "--run-dir", required=True, type=Path)
    export = sub.add_parser("export", help="assemble per-task records as evaluator JSONL, retaining missing rows")
    export.add_argument("--run", "--run-dir", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.dataset, args.output, args.seed, args.dev_ratio, args.pilot_ratio)
        elif args.command == "generate":
            protocol, tasks = make_protocol(
                args.prepared, args.model, read_json(args.model_lock), read_json(args.experiment),
                args.split, args.limit, args.tensor_parallel, args.gpu_memory_utilization,
                args.tokenize_only, num_shards=args.num_shards, shard_index=args.shard_index)
            protocol, attempt_environment = reconcile_resume_protocol(
                args.run, protocol, equivalent_hardware=args.resume_equivalent_hardware)
            open_run(args.run, protocol)  # reject mixed provenance before loading any model
            from .inference import VLLMBackend
            result = run_generation(args.run, protocol, tasks, VLLMBackend(protocol),
                                    attempt_environment=attempt_environment)
        elif args.command == "score":
            report = score_run(args.prepared, args.run)
            result = {key: report[key] for key in ("protocol_sha256", "selection_split", "selection_limit", "mode", "all_selected_tasks", "metric_scope")}
        elif args.command == "validate":
            manifest, tasks = load_prepared(args.prepared)
            from .core import digest
            if digest(manifest) != read_json(args.run / "protocol.json")["dataset_manifest_sha256"]:
                raise ProtocolError("validation dataset differs from frozen run")
            result = audit_summaries(args.run, validate_run(args.run, tasks))
        else:
            result = export_predictions(args.run, args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "validate" and not result["complete"]:
            return 2
        return 0
    except (ProtocolError, OSError, KeyError, ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
