#!/usr/bin/env bash
# Upload built PMTiles files to the MinIO reference-tiles bucket (created by minio-setup).
#
#   scripts/reference_layers/upload_pmtiles.sh [file.pmtiles ...]    (default: data/reference-tiles/*.pmtiles)
#
# Martin reads them from s3://reference-tiles/<name>.pmtiles (infra/docker/martin/config.yaml);
# restart Martin after uploading a new file. Needs the stack running and Docker.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; [[ -f .env ]] && source .env; set +a
files=("$@"); [[ ${#files[@]} -gt 0 ]] || files=(data/reference-tiles/*.pmtiles)
NETWORK=${STORAGE_NETWORK:-awakeforest-storage}
MC=${MC_IMAGE:-minio/mc:RELEASE.2025-08-13T08-35-41Z}
for f in "${files[@]}"; do
  f=$(realpath "$f"); echo "uploading $(basename "$f") ($(du -h "$f" | cut -f1))"
  docker run --rm --network "$NETWORK" -v "$(dirname "$f")":/in:ro \
    -e MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}" -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin_pass}" \
    --entrypoint /bin/sh "$MC" -c \
    'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
     mc mb --ignore-existing local/reference-tiles >/dev/null && mc cp --quiet /in/"'"$(basename "$f")"'" local/reference-tiles/'
done
docker run --rm --network "$NETWORK" -e MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}" -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin_pass}" \
  --entrypoint /bin/sh "$MC" -c 'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls --summarize local/reference-tiles'
