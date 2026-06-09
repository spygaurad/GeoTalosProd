"""
Machine-readable mirror of docs/paper/benchmark_task_spec.md (the LOCKED spec).

Single source of truth shared by every environment (Application / HPC / Colab) so
all of them reproduce the *identical* job. If a value changes here, change the doc too.
"""
from __future__ import annotations

# ─── Shared platform constants (identical for all tasks / environments) ──────
PLATFORM = {
    "patch_size_px": 1024,     # model_manager._DEFAULT_PATCH_SIZE
    "stride_px": 1024,         # no overlap (stride == patch)
    "max_patches": 5000,       # raised from default 1024 so full extent isn't truncated
    "patch_resolution": "native",   # COG native res via titiler PNG
    "imgsz": 800,              # YOLO/crown inference size
    "conf": 0.25,
    "iou": 0.7,                # detection + NMS
    "sam3_half": True,
}

# Benchmarks run at FULL dataset-item extent (the pinned AOIs were overhead-dominated).
# The naive runner ignores `aois` when full_extent is True and tiles the whole raster.
FULL_EXTENT = True

# ─── Infra handles (host side, where the docker app stack runs) ──────────────
INFRA = {
    "db_container": "awakeforest-app-db",
    "db_name": "geoplat",
    "db_user": "postgres",
    "minio_container": "awakeforest-minio",
    "minio_alias": "loc",   # http://localhost:9000 (minioadmin / minioadmin_pass)
    # containers worth sampling for the Application row (orchestration side)
    "sample_containers": ["awakeforest-worker-inference", "awakeforest-api"],
    "bucket": "org-7edefdc7-ebc2-4bf4-bff9-89dedbcee5bc",
}

# ─── The three pinned tasks ──────────────────────────────────────────────────
TASKS = {
    "task1": {
        "label": "Crown detection / YOLO",
        "pipeline_name": "Simple Crown Center Detection on a Dataset",
        "pipeline_name_benchmark": "Simple Crown Center Detection on a Dataset (benchmark)",
        "dataset_name": "Palms COG Orthomosaics",
        "dataset_id": "281401fa-e608-4cab-988f-0035f20ebfff",
        "item_id": "b273cb7f-7f98-4aae-9d40-4fe9368d06c4",
        "filename": "FCAT1_cog.tif",
        "s3_key": "datasets/281401fa-e608-4cab-988f-0035f20ebfff/"
                  "dc649a33c9bc87e4ba0ef4db3f38333122f04c4e81639f7dd2869b4a69909716_FCAT1_cog.tif",
        "size_bytes": 320_864_256,   # ~306 MiB
        "model": "Crown Center Detection",
        "endpoint": "/predict/crown/platform",
        "aois": {
            "AOI 4": [-79.72573938547748, 0.33776437219650346,
                      -79.72486385873476, 0.33826072314591893],
        },
        "prompts": None,
    },
    "task2": {
        "label": "Full-scene palm detection / YOLO",
        "pipeline_name": "Palm Yolo Multi AOI Detection with Report",
        "pipeline_name_benchmark": "Palm Yolo Multi AOI Detection with Report (benchmark)",
        "dataset_name": "Palms COG Orthomosaics",
        "dataset_id": "281401fa-e608-4cab-988f-0035f20ebfff",
        "item_id": "bd642e69-ec3b-49bf-a56b-2e9e094816b3",
        "filename": "JAMACOAQUE6_cog.tif",
        "s3_key": "datasets/281401fa-e608-4cab-988f-0035f20ebfff/"
                  "007ad3db1dfe04f97579724993612bb677692a8f1d41a3b7e1715175afa63f80_JAMACOAQUE6_cog.tif",
        "size_bytes": 155_189_248,   # ~148 MiB
        "model": "Yolo",
        "endpoint": "/predict/yolo/platform",
        "aois": {
            "AOI 1": [-80.11010259389879, -0.09756798797093429,
                      -80.10749816894533, -0.09603913101459735],
            "AOI 2": [-80.1070261001587, -0.09547586790812239,
                      -80.10398983955385, -0.0939523752692936],
        },
        "prompts": None,
    },
    "task3": {
        "label": "Multi-prompt segmentation / SAM3",
        "pipeline_name": "Single AOI SAM3 Multi-Prompt to isolate different environmental features",
        "pipeline_name_benchmark": "Single AOI SAM3 Multi-Prompt to isolate different environmental features (benchmark)",
        "dataset_name": "Kotsimba corrected cog",
        "dataset_id": "136739fd-e8c5-498e-8fff-abfc291773ac",
        "item_id": "aa70b803-b502-47df-b552-e0daa61246de",
        "filename": "Kotsimba_corrected_cog.tif",
        "s3_key": "datasets/136739fd-e8c5-498e-8fff-abfc291773ac/Kotsimba_corrected_cog.tif",
        "size_bytes": 4_666_628_963,   # 4.67 GB  (large-dataset / queueing-ablation task)
        "model": "SAM 3 - Mining Segmentation",
        "endpoint": "/segment/sam3/platform",
        "aois": {
            "AOI 1": [-70.2521985769272, -13.108664791812977,
                      -70.24939298629762, -13.106386842454722],
        },
        # 3 text-prompt passes over the same AOI/patches (drives the cache ablation)
        "prompts": [["crops", "farm crops"], ["water", "pond"], ["rooftop", "building"]],
    },
}


def get_task(key: str) -> dict:
    if key not in TASKS:
        raise KeyError(f"unknown task {key!r}; choose from {list(TASKS)}")
    return TASKS[key]
