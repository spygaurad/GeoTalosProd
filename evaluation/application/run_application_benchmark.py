"""
Application (GeoTALOS) benchmark harness — Setup 1.

Wrap-around, zero app-code changes:
  1. you trigger the pipeline from the UI (or curl with a token);
  2. this harness detects the new run, samples GPU + container resources during it;
  3. when the run finishes, it reads timing straight from Postgres and joins the two.

Usage (from repo root):
  python -m evaluation.application.run_application_benchmark --task task3 --label GeoTALOS
  python -m evaluation.application.run_application_benchmark --task task1 --run-id <uuid>   # post-hoc, timing only

Then click "Run" on the pipeline in the UI. The harness prints a result row and
appends it to evaluation/results/application.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from evaluation.task_spec import get_task, INFRA, FULL_EXTENT
from evaluation.common import db_metrics as dbm
from evaluation.common.resource_sampler import ResourceSampler

_RESULTS = os.path.join(os.path.dirname(__file__), "..", "results", "application.jsonl")
_TERMINAL = {"completed", "failed", "cancelled", "error"}
_ACTIVE = {"pending", "queued", "running"}


def _trigger(pipeline_name: str) -> None:
    """Autonomously trigger a run via the worker container (no UI/auth)."""
    print(f">>> Triggering '{pipeline_name}' via worker container ...", flush=True)
    subprocess.run(
        ["docker", "exec", INFRA["sample_containers"][0],
         "python", "-m", "evaluation.application.trigger_run", "--pipeline", pipeline_name],
        check=True,
    )


def _wait_for_new_run(pipeline_name: str, baseline: str | None,
                      poll: float, timeout: float) -> str:
    """Block until a run newer than `baseline` appears; return its id."""
    print(f"\n>>> Waiting for a new run of '{pipeline_name}' ...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        latest = dbm.latest_run_id(pipeline_name)
        if latest and latest != baseline:
            print(f">>> Detected new run {latest}", flush=True)
            return latest
        time.sleep(poll)
    sys.exit("Timed out waiting for a new run to start.")


def _wait_until_done(run_id: str, poll: float, timeout: float) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st = dbm.run_status(run_id)
        status = st["status"] if st else "unknown"
        if status != last:
            print(f"    run status: {status}", flush=True)
            last = status
        if status in _TERMINAL:
            return status
        time.sleep(poll)
    sys.exit("Timed out waiting for the run to finish.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="task1 | task2 | task3")
    ap.add_argument("--label", default="GeoTALOS", help="environment label for the row")
    ap.add_argument("--run-id", default=None,
                    help="analyze an existing run (timing only, no live resource sampling)")
    ap.add_argument("--trigger", action="store_true",
                    help="autonomously start the run via the worker container")
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=7200)
    ap.add_argument("--out", default=_RESULTS)
    args = ap.parse_args()

    task = get_task(args.task)
    # Full extent uses the cloned "(benchmark)" pipeline (no AOI, raised patch cap).
    pipeline_name = (task["pipeline_name_benchmark"] if FULL_EXTENT
                     else task["pipeline_name"])

    sampler_result: dict | None = None

    if args.run_id:
        # Post-hoc: no live resources, just DB timing for a finished run.
        run_id = args.run_id
        print(f">>> Post-hoc analysis of run {run_id} (no resource sampling)")
    else:
        baseline = dbm.latest_run_id(pipeline_name)
        sampler = ResourceSampler(containers=INFRA["sample_containers"]).start()
        if args.trigger:
            _trigger(pipeline_name)
        else:
            print(f"\n>>> Trigger '{pipeline_name}' from the UI now.", flush=True)
        run_id = _wait_for_new_run(pipeline_name, baseline, args.poll, args.timeout)
        try:
            status = _wait_until_done(run_id, args.poll, args.timeout)
        finally:
            sampler_result = sampler.stop()
        print(f">>> Run finished with status: {status}")

    metrics = dbm.compute_metrics(run_id)

    row = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": args.label,
        "task": args.task,
        "task_label": task["label"],
        "dataset": task["dataset_name"],
        "dataset_size_bytes": task["size_bytes"],
        "pipeline_name": pipeline_name,
        **metrics,
        "resources": sampler_result,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "a") as f:
        f.write(json.dumps(row) + "\n")

    _print_summary(row)


def _print_summary(row: dict) -> None:
    def s(ms): return f"{ms/1000:.2f}s" if ms else "-"
    print("\n" + "=" * 64)
    print(f"  {row['environment']}  |  {row['task_label']}")
    print("=" * 64)
    print(f"  dataset            : {row['dataset']} "
          f"({row['dataset_size_bytes']/1024**2:.0f} MiB)")
    print(f"  end-to-end runtime : {s(row['end_to_end_ms'])}")
    print(f"  dataset load time  : {s(row['dataset_load_ms'])}")
    print(f"  model time         : {s(row['model_time_ms'])}")
    print(f"  model runs         : {row['model_runs']}")
    print(f"  repeated data loads: {row['repeated_data_loads']}")
    print(f"  worker blocked time: {s(row['worker_blocked_ms'])}")
    res = row.get("resources")
    if res:
        print(f"  GPU                : {', '.join(res['gpu_names']) or '-'}")
        print(f"  peak GPU mem       : {res['peak_gpu_mem_mib']:.0f} MiB "
              f"(util {res['peak_gpu_util_pct']:.0f}%)")
        print(f"  PLATFORM peak CPU  : {res.get('peak_total_cpu_pct', 0):.0f}%  "
              f"RAM {res.get('peak_total_mem_mib', 0)/1024:.2f} GB  (summed across containers)")
        for name, c in res["containers"].items():
            print(f"  {name:<26}: CPU {c['peak_cpu_pct']:.0f}%  RAM {c['peak_mem_mib']:.0f} MiB")
    else:
        print("  resources          : (post-hoc; not sampled)")
    print("=" * 64)
    print(f"  saved -> {os.path.relpath(_RESULTS)}")


if __name__ == "__main__":
    main()
