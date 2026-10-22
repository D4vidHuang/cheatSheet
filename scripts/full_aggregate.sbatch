#!/bin/bash
#SBATCH --job-name=swe-full-summary
#SBATCH --partition=all
#SBATCH --account=daic-eemcs-st-se
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:30:00
set -euo pipefail
: "${SWE_ROOT:?Set immutable release root}"
: "${SWE_PYTHON:?Set verified Python interpreter}"
cd "$SWE_ROOT"
export PYTHONPATH="$SWE_ROOT/src" PYTHONUNBUFFERED=1
exec "$SWE_PYTHON" scripts/aggregate_full_run.py --root "$SWE_ROOT" \
  --plan configs/full_run.plan.json --output reports/full_aggregate.json
