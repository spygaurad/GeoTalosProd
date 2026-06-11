# Evaluation Setup — Compute & Queueing Benchmark

Replication guide for the paper's two tables:
- **`tab:compute-comparison`** — compute resources per environment (Application / HPC / Colab)
- **`tab:queueing-ablation`** — queueing + cache effect on the large dataset

This README is the entry point. Pinned task parameters live in `task_spec.py`
(human mirror: `../docs/paper/benchmark_task_spec.md`). Filled tables + prose:
`results/benchmark_tables.tex`.

---

## 1. What we measure and why

The system is two decoupled processes talking over HTTP:

```
  GeoTalosProd (the "application", CPU-only, docker)      palm_api (model server, GPU)
  ┌───────────────────────────────────────┐                 ┌──────────────────────────┐
  │ automation engine (pipeline DAG)       │   HTTP /platform │ uvicorn :8012            │
  │ celery-worker-inference, postgres,     │ ───────────────► │ YOLO + SAM2 + SAM3       │
  │ redis, minio, titiler                  │ ◄─────────────── │ (GreenMark/src/palm_api) │
  └───────────────────────────────────────┘   JSON+base64    └──────────────────────────┘
```

The **same job** (annotate a dataset item at full extent) is implemented three ways:

| Environment | Implementation | What it shows |
|---|---|---|
| **Application (GeoTALOS)** | the full platform runs the job as an automation pipeline | real platform cost: streaming, queueing, caching, georef, persistence |
| **HPC** | a naive standalone script (`naive/`) loads the scene, tiles it, calls the model in-process, georeferences by hand | bare-metal expert baseline on managed GPUs |
| **Colab** | the same naive script on a free/accessible GPU | accessibility baseline (large task **OOMs**) |

**Thesis (confirmed):** the platform is *slower* (2–4×) but uses **10–160× less RAM**
(streams tiles vs. naive bulk-loading the COG), with caching/queueing/automation on top.

To keep it a fair comparison the naive script **reuses palm_api's exact model code**
(copied verbatim into `naive/model_runners.py`) and the **same** `1024²` tiling /
`conf=0.25,iou=0.7,imgsz=800` params (`task_spec.py:PLATFORM`).

---

## 2. The three tasks (`task_spec.py`)

| Key | Workflow / model | Dataset item | COG | Endpoint |
|---|---|---|---|---|
| `task1` | Crown detection / YOLO-pose | FCAT1 (Palm) | 307 MB | `/predict/crown/platform` |
| `task2` | Full-scene detection / YOLO | JAMACOAQUE6 (Palm) | 148 MB | `/predict/yolo/platform` |
| `task3` | Multi-prompt seg / SAM3 (3 text prompts) | Kotsimba (ELDOR) | **4.67 GB** | `/segment/sam3/platform` |

All run at **full dataset-item extent** (`FULL_EXTENT=True`); the original pinned AOIs
were overhead-dominated. task3 is the large / ablation workload.

---

## 3. Directory layout

```
evaluation/
  evaluation_setup.md            ← this file
  task_spec.py                   ← locked spec: tasks, params, COG keys, infra handles
  clone_benchmark_pipelines.py   ← one-time: clone the 3 pipelines + models (full extent)
  build_tables.py                ← emit both LaTeX tables from the *.jsonl results
  common/
    db_metrics.py                ← read-only Postgres timing (no app code added)
    resource_sampler.py          ← nvidia-smi + docker stats + ru_maxrss sampler
  application/
    run_application_benchmark.py ← Application harness (sample + join DB)
    trigger_run.py               ← autonomous run trigger (runs inside worker container)
  naive/
    model_runners.py             ← crown/yolo/sam3 inference COPIED from palm_api
    run_naive_benchmark.py       ← naive runner: tile + georef + sample
  hpc/
    stage_data.sh                ← pull COGs from MinIO → shared NFS scratch
    submit_hpc.sbatch            ← SLURM launcher for the naive runner
  colab/
    colab_naive_benchmark.md     ← Colab guide (copy cmds + self-contained notebook code)
  results/
    application.jsonl  naive.jsonl    ← raw rows (one per run)
    tables.tex  benchmark_tables.tex  ← generated tables (+ interpretation)
    naive_*.geojson  slurm-*.out      ← per-run detections + SLURM logs
```

---

## 4. Prerequisites

| Need | Detail |
|---|---|
| Docker app stack up | `docker compose up -d` in repo root → `awakeforest-app-db` (:5435), `-worker-inference`, `-api`, `-minio` (:9002) |
| **palm_api running** | `uvicorn app:app --host 0.0.0.0 --port 8012` from `GreenMark/src/palm_api`, reachable at `host.docker.internal:8012` from the worker |
| Python env (naive/clone/build) | `greenmark_model_venv` (torch 2.9 — **required by SAM3**; `greenmark_venv`/torch 2.0 is too old). Has ultralytics 8.3.237, rasterio, psycopg2 |
| Weights on NFS | `GreenMark/models/{yolo11x-ortho,yolov11x-pose,sam3}.pt`, `bpe_simple_vocab_16e6.txt.gz` |
| SLURM access (HPC) | accounts `csc331`/`yanggrp`; partitions `gpu`, `gpu_small`, `yangGrp` |

Sanity check palm_api reachability from the worker:
```bash
docker exec awakeforest-worker-inference sh -c 'curl -s -o /dev/null -w "%{http_code}" http://host.docker.internal:8012/docs'
```

---

## 5. One-time setup

**5a. Clone the benchmark pipelines + models** (full-extent, raised patch cap; originals untouched):
```bash
greenmark_model_venv/bin/python evaluation/clone_benchmark_pipelines.py
```
Creates `… (benchmark)` pipelines (drop `aoi_id` → full item; task2 collapsed to one
branch) and `… (benchmark)` ai_models with `output_config.max_patches_per_item=5000`.
Idempotent — re-running skips existing clones.

**5b. Stage the COGs onto shared NFS** (for HPC/naive; Kotsimba is usually already on disk):
```bash
bash evaluation/hpc/stage_data.sh
# pulls FCAT1 + JAMACOAQUE6 from MinIO (host :9002) to
# datasets/data/dataset_benchmark_cog/  (visible from every SLURM node)
```

---

## 6. Run the **Application (GeoTALOS)** rows

The harness records the run's baseline, starts the sampler, triggers the cloned pipeline
autonomously (via the worker container — no UI/auth), waits for completion, then reads
timing straight from Postgres (`automation_runs` / `_run_steps`) and joins the peaks.

```bash
export PYTHONPATH=$PWD
PY=/home/prass25/projects/greenmark_model_venv/bin/python
for T in task1 task2 task3; do
  $PY -m evaluation.application.run_application_benchmark --task $T --label GeoTALOS --trigger
done
```
- task3 is the long one (~35 min: ~8,900 patch POSTs over 3 prompts).
- Drop `--trigger` to instead wait for a manual UI "Run" click.
- `--run-id <uuid>` does post-hoc timing only (no live resource sampling).
- Rows appended to `results/application.jsonl`.

**palm_api must be up on the host** (Application rows call it; the GeoTALOS GPU = wherever
palm_api runs, here the L40S box). Run Application rows **when no naive job shares that GPU**.

---

## 7. Run the **HPC (naive)** rows

The naive runner loads the whole scene, tiles it `1024²`, runs the model in-process,
georeferences, and samples GPU (`nvidia-smi`) + peak RSS (`ru_maxrss`).

**Submit per (task × GPU).** Pick a GPU with `--gres`; `submit_hpc.sbatch` does 1 warm-up
(discarded) + `RUNS` timed runs (default 1):
```bash
# A100 (dedicated nodes) and H200 (gpu_small)
for T in task1 task2 task3; do
  TIME=01:00:00; [ "$T" = task3 ] && TIME=02:30:00
  sbatch -A csc331 -p gpu       --gres=gpu:A100_40:1 -t $TIME --export=ALL,TASK=$T,GPU_LABEL=A100_40 evaluation/hpc/submit_hpc.sbatch
done
```
- GPU options (skip 12 GB cards — can't hold the models): `A100_40`, `A100_80`, `H200_141`, `L40S`, `V100_32`.
- The runner is plain Python (no docker on compute nodes); weights + code are on NFS.
- Rows → `results/naive.jsonl`; detections → `results/naive_<task>_<gpu>.geojson`; logs → `results/slurm-*.out`.

**Running the L40S row on the lovelace login box directly** (when palm_api occupies one GPU):
```bash
export CUDA_VISIBLE_DEVICES=1 PYTHONPATH=$PWD   # pin a GPU palm_api isn't on (it's on 0)
$PY -m evaluation.naive.run_naive_benchmark --task task3 --gpu-label L40S \
    --data-dir datasets/data/dataset_benchmark_cog
```
The sampler honors `CUDA_VISIBLE_DEVICES`, so it reads only the pinned GPU.

**Cache toggle (for the ablation):** add `--cache` to read the scene once and reuse across
prompts (the default, no flag, is the naive reload-per-prompt behavior the ablation reports).

---

## 8. Run the **Colab** rows

See **`colab/colab_naive_benchmark.md`** — copy commands (lovelace → Google Drive via
rclone/scp), the mount/stage cells, and a self-contained notebook port of the naive runner
(same weights/tiling/params). task1/task2 complete on a T4/L4; **task3 is expected to OOM**
(needs 60–101 GB RAM vs Colab's ~12–51 GB) — that OOM is the reportable Colab result.

---

## 9. Build the tables

```bash
greenmark_model_venv/bin/python evaluation/build_tables.py            # prints LaTeX
greenmark_model_venv/bin/python evaluation/build_tables.py > evaluation/results/tables.tex
```
- Selects **canonical clean rows**: naive filtered by full-extent tile count; app filtered to
  `(benchmark)` pipelines; latest `captured_at` per `(env, task, cache)` wins (so clean reruns
  beat contaminated earlier ones). `EXCLUDE_GPUS` drops H200 (see §10).
- `results/benchmark_tables.tex` is the hand-finished version (tables + interpretation).

### Metric → source
| Metric | Application | Naive |
|---|---|---|
| End-to-end | `automation_runs.completed_at − started_at` | wall clock |
| Dataset load | `select_data_source` step `duration_ms` (~0; streamed) | time to read the scene |
| Model runs | `run_inference` node count † | per-tile calls |
| Repeated loads | count of `data_source` steps (=1, cached) | reads per run (3 for task3) |
| Peak RAM | `docker stats` worker-inference | `ru_maxrss` |
| Peak GPU | `nvidia-smi` (palm_api host) | `nvidia-smi` (pinned GPU) |
| Worker blocked | Σ step `duration_ms` (cumulative; < wall when parallel) | = end-to-end (sequential) |

† platform per-patch calls equal the naive count (same tiling); the DB stores nodes, not patches.

---

## 10. Gotchas (learned the hard way)

- **Isolate the L40S.** The GeoTALOS box and an "HPC L40S" row share lovelace. Don't run a
  naive L40S job while an Application run uses palm_api on the same physical GPU — pin them to
  different GPUs (`CUDA_VISIBLE_DEVICES`) or serialize. `scontrol hold` *after* submit races;
  submit with `--hold` if you need to gate.
- **H200 was on a shared `gpu_small` node** → its GPU-mem (impossible 71 GB) and task3 runtime
  were contaminated by a co-tenant. Excluded from the tables (`build_tables.EXCLUDE_GPUS`). Use
  dedicated A100 nodes + an isolated L40S GPU for clean numbers, or request exclusive H200.
- **Naive peak RAM depends on the mem cap.** Under SLURM `--mem=64G` task3 stayed ~60 GB;
  unconstrained on lovelace it ballooned to ~101 GB (its true demand). Report consistently.
- **SAM3 needs torch ≥ 2.9** (`greenmark_model_venv`); `greenmark_venv` (2.0) fails to import.
- **`naive.jsonl` accumulates** validation/contaminated rows — `build_tables.py` dedupes to
  canonical, but eyeball the `% ---- coverage ----` footer it prints.
- **Determinism:** YOLO detections match exactly across GPUs; SAM3 varies <0.2% (fp16 × arch).

---

## 11. Quick full replay

```bash
cd GeoTalosProd
PY=/home/prass25/projects/greenmark_model_venv/bin/python
export PYTHONPATH=$PWD

# 0. prereqs: docker stack up, palm_api up on :8012
$PY evaluation/clone_benchmark_pipelines.py          # one-time
bash evaluation/hpc/stage_data.sh                    # one-time

# 1. Application rows (palm_api on host; no competing GPU job)
for T in task1 task2 task3; do
  $PY -m evaluation.application.run_application_benchmark --task $T --label GeoTALOS --trigger; done

# 2. HPC rows (per GPU)
for T in task1 task2 task3; do
  sbatch -A csc331 -p gpu --gres=gpu:A100_40:1 -t 02:30:00 \
    --export=ALL,TASK=$T,GPU_LABEL=A100_40 evaluation/hpc/submit_hpc.sbatch; done

# 3. Colab rows: follow evaluation/colab/colab_naive_benchmark.md

# 4. tables
$PY evaluation/build_tables.py > evaluation/results/tables.tex
```
