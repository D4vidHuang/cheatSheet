#!/bin/bash
#SBATCH --job-name=swe-refactor-qwen
#SBATCH --partition=all
#SBATCH --account=daic-eemcs-st-se
#SBATCH --gres=gpu:nvidia_rtx_pro_6000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=02:00:00

set -euo pipefail
: "${SWE_ROOT:?Set dedicated remote experiment root}"
: "${SWE_PYTHON:?Set a verified compatible Python interpreter}"
: "${SWE_HF_HOME:?Set a model cache containing pinned revisions}"
: "${SWE_MODEL:?Set model alias from configs/models.lock.json}"
cd "$SWE_ROOT"
SWE_JOB_DIR="runs/${SWE_RUN_KIND:-smoke}/${SWE_MODEL}-${SLURM_JOB_ID}"
SWE_RUN="$SWE_JOB_DIR/model"
mkdir -p "$SWE_JOB_DIR"
export PYTHONPATH="$SWE_ROOT/src"
export HF_HOME="$SWE_HF_HOME"
export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export VLLM_NO_USAGE_STATS=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export PYTHONUNBUFFERED=1
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/swe-refactor-${SLURM_JOB_ID}/cache"
export TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton"
export CUDA_CACHE_PATH="$XDG_CACHE_HOME/cuda"
mkdir -p "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH"

finish() {
    local swe_exit=$?
    printf '%s\n' "$swe_exit" > "$SWE_JOB_DIR/exit_code.txt"
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$SWE_JOB_DIR/finished_at.txt"
    nvidia-smi > "$SWE_JOB_DIR/gpu-final.txt" 2>&1 || true
}
trap finish EXIT
hostname > "$SWE_JOB_DIR/hostname.txt"
nvidia-smi > "$SWE_JOB_DIR/gpu-start.txt"
"$SWE_PYTHON" -m pip freeze > "$SWE_JOB_DIR/environment.txt"
git rev-parse HEAD > "$SWE_JOB_DIR/git-head.txt" 2>/dev/null || true
SWE_ARGS=(--prepared prepared/swe-refactor --model "$SWE_MODEL"
          --run "$SWE_RUN" --split "${SWE_SPLIT:-pilot}")
if [[ -n "${SWE_LIMIT:-}" ]]; then
    SWE_ARGS+=(--limit "$SWE_LIMIT")
fi
"$SWE_PYTHON" -m swe_refactor generate "${SWE_ARGS[@]}" > "$SWE_JOB_DIR/runner.log" 2>&1
"$SWE_PYTHON" -m swe_refactor validate --prepared prepared/swe-refactor --run "$SWE_RUN" > "$SWE_JOB_DIR/validation.json"
"$SWE_PYTHON" -m swe_refactor score --prepared prepared/swe-refactor --run "$SWE_RUN" > "$SWE_JOB_DIR/score-console.json"
"$SWE_PYTHON" -m swe_refactor export --run "$SWE_RUN" --output "$SWE_JOB_DIR/predictions.jsonl" > "$SWE_JOB_DIR/export.json"
