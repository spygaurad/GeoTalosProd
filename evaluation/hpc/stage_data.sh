#!/usr/bin/env bash
# Stage the 3 benchmark COGs onto shared NFS (visible from every SLURM node).
# Kotsimba is already on disk; FCAT1 + JAMACOAQUE6 are pulled from MinIO.
#
#   bash evaluation/hpc/stage_data.sh [DEST_DIR]
set -euo pipefail

DEST="${1:-/home/prass25/projects/AwakeForest/datasets/data/dataset_benchmark_cog}"
MC="${MC:-$HOME/bin/mc}"
ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9002}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASS="${MINIO_PASS:-minioadmin_pass}"
BUCKET="org-7edefdc7-ebc2-4bf4-bff9-89dedbcee5bc"

mkdir -p "$DEST"
"$MC" alias set afbench "$ENDPOINT" "$MINIO_USER" "$MINIO_PASS" >/dev/null

# local_name -> s3 key under the bucket
declare -A KEYS=(
  ["FCAT1_cog.tif"]="datasets/281401fa-e608-4cab-988f-0035f20ebfff/dc649a33c9bc87e4ba0ef4db3f38333122f04c4e81639f7dd2869b4a69909716_FCAT1_cog.tif"
  ["JAMACOAQUE6_cog.tif"]="datasets/281401fa-e608-4cab-988f-0035f20ebfff/007ad3db1dfe04f97579724993612bb677692a8f1d41a3b7e1715175afa63f80_JAMACOAQUE6_cog.tif"
  ["Kotsimba_corrected_cog.tif"]="datasets/136739fd-e8c5-498e-8fff-abfc291773ac/Kotsimba_corrected_cog.tif"
)

for name in "${!KEYS[@]}"; do
  out="$DEST/$name"
  if [ -f "$out" ]; then
    echo "have   $name ($(du -h "$out" | cut -f1))"
  else
    echo "pull   $name ..."
    "$MC" cp "afbench/$BUCKET/${KEYS[$name]}" "$out"
  fi
done
echo "staged into: $DEST"
