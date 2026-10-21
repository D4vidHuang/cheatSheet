#!/bin/bash
#SBATCH --job-name=swe-full
#SBATCH --partition=all
#SBATCH --account=daic-eemcs-st-se
#SBATCH --gres=gpu:nvidia_rtx_pro_6000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=12:00:00
#SBATCH --signal=B:TERM@120
#SBATCH --requeue
set -euo pipefail
: "${SWE_ROOT:?Set immutable release root}"
: "${SWE_PYTHON:?Set verified Python interpreter}"
: "${SWE_HF_HOME:?Set pinned model cache}"
: "${SWE_MODEL:?Set model alias}"
: "${SLURM_ARRAY_TASK_ID:?This is a full-run array job}"
cd "$SWE_ROOT"
export PYTHONPATH="$SWE_ROOT/src"
export HF_HOME="$SWE_HF_HOME" HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false VLLM_NO_USAGE_STATS=1
export VLLM_USE_FLASHINFER_SAMPLER=0 PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=8
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/swe-full-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}/cache"
export TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton" CUDA_CACHE_PATH="$XDG_CACHE_HOME/cuda"
mkdir -p "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH"
exec "$SWE_PYTHON" scripts/full_array_worker.py --root "$SWE_ROOT" \
  --plan configs/full_run.plan.json --model "$SWE_MODEL" --shard-index "$SLURM_ARRAY_TASK_ID"
