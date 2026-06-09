"""
Build the two LaTeX tables from results/application.jsonl + results/naive.jsonl.

Selects canonical CLEAN rows:
  - full extent only (model_runs above a per-task threshold drops old small-AOI runs)
  - latest captured_at per (environment, task, cache)  -> clean reruns win

  python evaluation/build_tables.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(__file__)
RES = os.path.join(HERE, "results")

# model_runs floor per task to exclude pre-full-extent / partial runs
MIN_TILES = {"task1": 300, "task2": 200, "task3": 1000}
TASK_ORDER = ["task1", "task2", "task3"]
TASK_DATASET = {"task1": "Palm", "task2": "Palm", "task3": "ELDOR (Kotsimba)"}
TASK_WORKFLOW = {"task1": "Crown detection", "task2": "Full-scene detection",
                 "task3": "Multi-prompt segmentation"}
TASK_MODEL = {"task1": "YOLO", "task2": "YOLO", "task3": "SAM3"}


def _load(path):
    rows = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _ok_naive(r):
    # drop pre-full-extent / small-AOI naive runs by tile count
    return r.get("model_runs", 0) >= MIN_TILES.get(r["task"], 0)


def _ok_app(r):
    # only full-extent runs use the cloned "(benchmark)" pipeline
    return str(r.get("pipeline_name", "")).endswith("(benchmark)")


def _latest(rows, key):
    best = {}
    for r in rows:
        k = key(r)
        if k not in best or r["captured_at"] > best[k]["captured_at"]:
            best[k] = r
    return best


def _ms(v):
    return "--" if v in (None, 0) else f"{v/1000:.1f}"


def main():
    naive = [r for r in _load(os.path.join(RES, "naive.jsonl")) if _ok_naive(r)]
    app = [r for r in _load(os.path.join(RES, "application.jsonl")) if _ok_app(r)]

    # canonical: latest per (task, gpu_label, cache) for naive; latest per task for app
    n = _latest(naive, lambda r: (r["task"], r.get("gpu_label", ""), r.get("cache", False)))
    a = _latest(app, lambda r: r["task"])

    # H200 ran on a shared gpu_small node; its GPU-mem (and task3 runtime) were
    # contaminated by a co-tenant process. Use the dedicated/isolated GPUs only.
    EXCLUDE_GPUS = {"H200_141"}
    gpus = sorted({k[1] for k in n if k[1] not in EXCLUDE_GPUS})

    # ─── Table 1: compute comparison ────────────────────────────────────────
    print("% ===== Table 1: compute comparison =====")
    print("% cols: Dataset Workflow Model Environment GPU PeakCPU PeakRAM LoadTime E2E")
    for task in TASK_ORDER:
        rows_for_task = []
        # HPC sub-rows (one per GPU)
        for g in gpus:
            r = n.get((task, g, False))
            if not r:
                continue
            res = r["resources"]
            rows_for_task.append((
                "HPC", g,
                f"{res.get('peak_self_cpu_pct') or 0:.0f}\\%",
                f"{(res.get('peak_self_rss_mib') or 0)/1024:.1f}",
                _ms(r["dataset_load_ms"]), _ms(r["end_to_end_ms"]),
            ))
        # GeoTALOS row
        ar = a.get(task)
        if ar:
            res = ar.get("resources") or {}
            wc = (res.get("containers") or {}).get("awakeforest-worker-inference", {})
            rows_for_task.append((
                "GeoTALOS", ", ".join(res.get("gpu_names") or ["L40S"]),
                f"{wc.get('peak_cpu_pct', 0):.0f}\\%",
                f"{wc.get('peak_mem_mib', 0)/1024:.2f}",
                _ms(ar["dataset_load_ms"]), _ms(ar["end_to_end_ms"]),
            ))
        # Colab placeholder
        rows_for_task.append(("Colab", "[TBD]", "[v]", "[v]", "[v]", "[v]"))

        for env, gpu, cpu, ram, load, e2e in rows_for_task:
            print(f"{TASK_DATASET[task]} & {TASK_WORKFLOW[task]} & {TASK_MODEL[task]} & "
                  f"{env} & {gpu} & {cpu} & {ram}\\,GB & {load}\\,s & {e2e}\\,s \\\\")
        print("\\midrule")

    # ─── Table 2: queueing + cache ablation (task3, large dataset) ───────────
    print("\n% ===== Table 2: queueing + cache ablation (task3) =====")
    # naive: prefer L40S (same GPU as GeoTALOS) else any
    naive_t3 = n.get(("task3", "L40S", False)) or next(
        (n[k] for k in n if k[0] == "task3" and not k[2]), None)
    app_t3 = a.get("task3")

    def emit(label, e2e, model_runs, loads, ram_gb, blocked, size="4.67\\,GB"):
        print(f"{label} & {size} & {model_runs} & {loads} & {ram_gb}\\,GB & "
              f"{blocked}\\,s & {e2e}\\,s \\\\")

    if naive_t3:
        res = naive_t3["resources"]
        emit("Naive script", _ms(naive_t3["end_to_end_ms"]),
             naive_t3["model_runs"], naive_t3["repeated_data_loads"],
             f"{(res.get('peak_self_rss_mib') or 0)/1024:.1f}",
             _ms(naive_t3["worker_blocked_ms"]))
    if app_t3:
        res = app_t3.get("resources") or {}
        wc = (res.get("containers") or {}).get("awakeforest-worker-inference", {})
        # model runs equal by construction (same tiling); use naive's count if known
        mruns = naive_t3["model_runs"] if naive_t3 else app_t3["model_runs"]
        emit("GeoTALOS queue + cache", _ms(app_t3["end_to_end_ms"]),
             f"{mruns}\\,$^\\dagger$", app_t3["repeated_data_loads"],
             f"{wc.get('peak_mem_mib', 0)/1024:.2f}",
             _ms(app_t3["worker_blocked_ms"]))
    print("% $\\dagger$ platform per-patch model calls equal the naive count "
          "(same 1024px tiling); the DB records 3 run_inference nodes, not per-patch calls.")

    # ─── coverage summary ───────────────────────────────────────────────────
    print("\n% ---- coverage ----")
    for task in TASK_ORDER:
        have = [g for g in gpus if (task, g, False) in n]
        print(f"%   {task}: naive GPUs={have}  geotalos={'Y' if task in a else 'N'}")


if __name__ == "__main__":
    main()
