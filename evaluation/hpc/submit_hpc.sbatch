#!/usr/bin/env bash
# Naive baseline on an HPC GPU node (Setup 2).
# Everything (code, weights, COGs) is on shared NFS, so nothing is copied here —
# we just allocate a GPU and run the in-process naive script.
#
# Submit (pick a GPU series via --gres + a matching partition):
#   sbatch -p gpu_small --gres=gpu:A100_80:1 \
#          --export=ALL,TASK=task3,GPU_LABEL=A100_80 evaluation/hpc/submit_hpc.sbatch
#
#   sbatch -p gpu       --gres=gpu:V100_32:1 \
#          --export=ALL,TASK=task1,GPU_LABEL=V100_32 evaluation/hpc/submit_hpc.sbatch
#
# GPU options (from `sinfo`, skip 12GB cards): V100_32, A100_40, A100_80, H200_141, L40S
#
#SBATCH --job-name=af-naive
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=01:00:00
#SBATCH --output=evaluation/results/slurm-%j.out
set -euo pipefail

: "${TASK:?set TASK=task1|task2|task3}"
: "${GPU_LABEL:?set GPU_LABEL=e.g. A100_80}"
DATA_DIR="${DATA_DIR:-/home/prass25/projects/AwakeForest/datasets/data/dataset_benchmark_cog}"
PYTHON="${PYTHON:-/home/prass25/projects/greenmark_model_venv/bin/python}"  # torch 2.9 — required by SAM3
REPO="${REPO:-/home/prass25/projects/AwakeForest/AwakeForestProd}"

echo "node=$SLURMD_NODENAME gpu=$GPU_LABEL task=$TASK"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
free -h | head -2

cd "$REPO"
export CUDA_VISIBLE_DEVICES=0
export PYTHONPATH="$REPO:${PYTHONPATH:-}"

# warm-up (CUDA init + weight load + lazy SAM3 predictor build) — discarded
"$PYTHON" -m evaluation.naive.run_naive_benchmark \
    --task "$TASK" --gpu-label "$GPU_LABEL" --data-dir "$DATA_DIR" --warmup

# timed runs (RUNS=1 by default; raise for cheap tasks if you want a median)
RUNS="${RUNS:-1}"
for i in $(seq 1 "$RUNS"); do
  echo ">>> timed run $i/$RUNS"
  "$PYTHON" -m evaluation.naive.run_naive_benchmark \
      --task "$TASK" --gpu-label "$GPU_LABEL" --data-dir "$DATA_DIR"
done
