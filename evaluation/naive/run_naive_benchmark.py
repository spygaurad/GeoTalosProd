"""
Naive baseline — Setup 2 (HPC / Colab).

A straightforward standalone script: open the COG, clip the AOI, tile it (1024 px,
native res, no overlap — identical to the platform), run the model IN-PROCESS, and
georeference results by hand. No DB, no queue, no cache, no HTTP.

This is the "researcher's script" the platform is compared against. It deliberately
re-reads/re-tiles the COG for every pass (each AOI, and for SAM3 each text prompt),
which is what makes the queueing+cache ablation meaningful. Pass --cache to read once.

Usage (under SLURM on a GPU node):
  python -m evaluation.naive.run_naive_benchmark --task task3 --gpu-label A100_80 \
         --data-dir /scratch/$USER/af_cogs

Writes one row to evaluation/results/naive.jsonl plus a GeoJSON of detections.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds
from rasterio.warp import transform as warp_transform, transform_bounds

from evaluation.task_spec import get_task, PLATFORM, FULL_EXTENT
from evaluation.common.resource_sampler import ResourceSampler
from evaluation.naive import model_runners as mr

_RESULTS = os.path.join(os.path.dirname(__file__), "..", "results", "naive.jsonl")


def _read_rgb_window(ds, window: Window) -> np.ndarray:
    """Read a window as uint8 HxWx3 RGB (first 3 bands)."""
    n = min(3, ds.count)
    arr = ds.read(list(range(1, n + 1)), window=window)        # (bands, h, w)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if n == 1:
        arr = np.repeat(arr, 3, axis=0)
    return np.transpose(arr, (1, 2, 0))                         # (h, w, 3)


def _aoi_pixel_window(ds, bbox4326) -> Window:
    """Reproject a 4326 bbox into the dataset CRS and return the pixel window."""
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", ds.crs, *bbox4326)
    win = from_bounds(minx, miny, maxx, maxy, ds.transform)
    return win.round_offsets().round_lengths()


def _pixels_to_4326(ds, abs_xs, abs_ys):
    """Absolute pixel coords -> dataset CRS -> lon/lat (vectorised)."""
    xs, ys = rasterio.transform.xy(ds.transform, abs_ys, abs_xs)  # row=y, col=x
    lon, lat = warp_transform(ds.crs, "EPSG:4326", np.atleast_1d(xs), np.atleast_1d(ys))
    return lon, lat


def _georef_instance(ds, inst, off_x, off_y):
    """Convert an instance's patch-pixel geometry to a GeoJSON geometry in 4326."""
    def conv(ring):
        axs = [off_x + p[0] for p in ring]
        ays = [off_y + p[1] for p in ring]
        lon, lat = _pixels_to_4326(ds, axs, ays)
        return [[lo, la] for lo, la in zip(lon, lat)]

    if "point" in inst:
        lon, lat = _pixels_to_4326(ds, [off_x + inst["point"][0]], [off_y + inst["point"][1]])
        return {"type": "Point", "coordinates": [lon[0], lat[0]]}
    if inst.get("polygons"):       # SAM3 (multi-ring)
        return {"type": "MultiPolygon",
                "coordinates": [[conv(r) for r in poly] for poly in inst["polygons"]]}
    if inst.get("polygon"):        # YOLO box as polygon
        ring = inst["polygon"] + [inst["polygon"][0]]
        return {"type": "Polygon", "coordinates": [conv(ring)]}
    return None


def _tile_offsets(width, height, patch, stride):
    xs = list(range(0, max(1, width - patch + 1), stride)) or [0]
    ys = list(range(0, max(1, height - patch + 1), stride)) or [0]
    return [(x, y) for y in ys for x in xs]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--gpu-label", default="", help="e.g. A100_80 / H200_141 / V100_32")
    ap.add_argument("--data-dir", required=True, help="dir holding the staged COG")
    ap.add_argument("--cache", action="store_true",
                    help="read the AOI once and reuse across passes (defeats the ablation)")
    ap.add_argument("--warmup", action="store_true", help="discard this run (weight/CUDA init)")
    ap.add_argument("--out", default=_RESULTS)
    args = ap.parse_args()

    task = get_task(args.task)
    cog_path = os.path.join(args.data_dir, task["filename"])
    if not os.path.exists(cog_path):
        raise SystemExit(f"COG not found: {cog_path} (run stage_data.sh first)")

    endpoint = task["endpoint"]
    runner_name, runner = mr.RUNNERS[endpoint]
    # passes = (region, prompts) units; SAM3 -> one pass per prompt group.
    # Full extent: one region = whole raster (bbox=None). Otherwise one per AOI.
    prompt_groups = task["prompts"] or [None]
    if FULL_EXTENT:
        regions = [("full", None)]
    else:
        regions = list(task["aois"].items())
    passes = [(name, bbox, pg) for name, bbox in regions for pg in prompt_groups]

    P = PLATFORM
    sampler = ResourceSampler(sample_gpu=True, sample_self=True).start()
    t0 = time.time()

    data_load_s = 0.0
    data_loads = 0
    model_runs = 0
    features = []
    cached = {}  # aoi -> (window, tiles) when --cache

    for aoi, bbox, pg in passes:
        with rasterio.open(cog_path) as ds:
            if args.cache and aoi in cached:
                win, tiles = cached[aoi]
            else:
                t_load = time.time()
                win = (Window(0, 0, ds.width, ds.height) if bbox is None
                       else _aoi_pixel_window(ds, bbox))
                region = _read_rgb_window(ds, win)          # whole region into RAM (the naive cost)
                offsets = _tile_offsets(region.shape[1], region.shape[0],
                                        P["patch_size_px"], P["stride_px"])
                offsets = offsets[:P["max_patches"]]        # match platform's patch cap
                tiles = [(ox, oy, region[oy:oy + P["patch_size_px"],
                                         ox:ox + P["patch_size_px"]]) for ox, oy in offsets]
                data_load_s += time.time() - t_load
                data_loads += 1
                if args.cache:
                    cached[aoi] = (win, tiles)

            win_off_x, win_off_y = int(win.col_off), int(win.row_off)
            for ox, oy, tile in tiles:
                if tile.shape[0] < 8 or tile.shape[1] < 8:
                    continue
                if runner_name == "sam3":
                    instances = runner(tile, pg)
                else:
                    instances = runner(tile, conf=P["conf"], iou=P["iou"])
                model_runs += 1
                for inst in instances:
                    geom = _georef_instance(ds, inst, win_off_x + ox, win_off_y + oy)
                    if geom is None:
                        continue
                    features.append({
                        "type": "Feature", "geometry": geom,
                        "properties": {"label": inst["label"], "score": inst["score"],
                                       "aoi": aoi, "prompt": pg},
                    })

    end_to_end_s = time.time() - t0
    res = sampler.stop()

    if args.warmup:
        print(">>> warmup run complete (discarded)")
        return

    # write detections
    geojson_path = os.path.join(os.path.dirname(args.out),
                                f"naive_{args.task}_{args.gpu_label or 'gpu'}.geojson")
    with open(geojson_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)

    row = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": f"HPC:{args.gpu_label}" if args.gpu_label else "naive",
        "task": args.task,
        "task_label": task["label"],
        "dataset": task["dataset_name"],
        "dataset_size_bytes": task["size_bytes"],
        "gpu_label": args.gpu_label,
        "slurm_node": os.environ.get("SLURMD_NODENAME"),
        "cache": args.cache,
        "end_to_end_ms": round(end_to_end_s * 1000),
        "dataset_load_ms": round(data_load_s * 1000),
        "model_time_ms": None,           # folded into end-to-end (sequential, in-process)
        "model_runs": model_runs,
        "repeated_data_loads": data_loads,
        "worker_blocked_ms": round(end_to_end_s * 1000),  # single process: blocked = runtime
        "features": len(features),
        "resources": res,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "a") as f:
        f.write(json.dumps(row) + "\n")
    _print_summary(row, geojson_path)


def _print_summary(row, geojson_path):
    def s(ms): return f"{ms/1000:.2f}s" if ms else "-"
    r = row["resources"]
    print("\n" + "=" * 64)
    print(f"  {row['environment']}  |  {row['task_label']}  (cache={row['cache']})")
    print("=" * 64)
    print(f"  dataset            : {row['dataset']} ({row['dataset_size_bytes']/1024**2:.0f} MiB)")
    print(f"  end-to-end runtime : {s(row['end_to_end_ms'])}")
    print(f"  dataset load time  : {s(row['dataset_load_ms'])}")
    print(f"  model runs (tiles) : {row['model_runs']}")
    print(f"  repeated data loads: {row['repeated_data_loads']}")
    print(f"  worker blocked time: {s(row['worker_blocked_ms'])}")
    print(f"  features           : {row['features']}")
    print(f"  GPU                : {', '.join(r['gpu_names']) or '-'}")
    print(f"  peak GPU mem       : {r['peak_gpu_mem_mib']:.0f} MiB (util {r['peak_gpu_util_pct']:.0f}%)")
    print(f"  peak RAM (RSS)     : {r['peak_self_rss_mib']:.0f} MiB  CPU {r['peak_self_cpu_pct']:.0f}%")
    print("=" * 64)
    print(f"  saved -> {os.path.relpath(row['out']) if 'out' in row else _RESULTS}")
    print(f"  geojson -> {os.path.relpath(geojson_path)}")


if __name__ == "__main__":
    main()
