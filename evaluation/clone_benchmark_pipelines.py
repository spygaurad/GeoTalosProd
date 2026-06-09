"""
One-time setup: clone the 3 source pipelines (+ their models) into full-extent
"(benchmark)" variants so the Application row measures the SAME job as the naive
script. Originals and the UI are left untouched.

Transforms per clone:
  - ai_model      -> clone with output_config.max_patches_per_item = 5000
  - select_data_source -> drop `aoi_id` (full item)
  - run_inference -> repoint model_id to the cloned model
  - drop optional `selection->aoi` edges (run on full item, like the crown pipeline)
  - task2 only    -> remove one of the two parallel AOI branches (full-scene single pass)

Idempotent: re-running skips clones that already exist.

  /home/prass25/projects/greenmark_model_venv/bin/python evaluation/clone_benchmark_pipelines.py
"""
from __future__ import annotations

import json
import uuid

import psycopg2
import psycopg2.extras

DSN = dict(host="localhost", port=5435, dbname="geoplat", user="postgres", password="postgres")
SUFFIX = " (benchmark)"
MAX_PATCHES = 5000

SOURCES = {
    "task1": "Simple Crown Center Detection on a Dataset",
    "task2": "Palm Yolo Multi AOI Detection with Report",
    "task3": "Single AOI SAM3 Multi-Prompt to isolate different environmental features",
}
# task2: branch to delete (the 2nd AOI branch) — keep 972c -> dfb4 -> overlay+report
TASK2_DROP_NODES = {
    "bc271ddf-ae1c-4795-ace3-586560d9d5cd",  # select_data_source AOI2
    "c02f81d8-ddb2-454d-a10f-cd506299c698",  # run_inference for AOI2
    "7cba0193-9705-4a4b-a403-e00b555b6f9d",  # overlay for AOI2
}


def _node_type(n):
    return (n.get("data") or {}).get("nodeType") or n.get("type")


def clone_model(cur, orig_id: str, model_map: dict) -> str:
    if orig_id in model_map:
        return model_map[orig_id]
    cur.execute("SELECT output_config, name FROM ai_models WHERE id=%s", (orig_id,))
    output_config, name = cur.fetchone()
    output_config = dict(output_config or {})
    output_config["max_patches_per_item"] = MAX_PATCHES
    new_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO ai_models
          (id, organization_id, created_by, created_at, updated_at, annotation_schema_id,
           request_config, auth_config, input_schema, output_schema, config, name,
           description, framework, version, type, endpoint_url, output_config)
        SELECT %s, organization_id, created_by, now(), now(), annotation_schema_id,
           request_config, auth_config, input_schema, output_schema, config, %s,
           description, framework, version, type, endpoint_url, %s
        FROM ai_models WHERE id=%s
        """,
        (new_id, name + SUFFIX, json.dumps(output_config), orig_id),
    )
    model_map[orig_id] = new_id
    print(f"    cloned model {name!r} -> {new_id} (max_patches={MAX_PATCHES})")
    return new_id


def transform_graph(graph: dict, task: str, cur, model_map: dict) -> dict:
    nodes, edges = graph["nodes"], graph["edges"]
    drop = TASK2_DROP_NODES if task == "task2" else set()

    new_nodes = []
    for n in nodes:
        if n["id"] in drop:
            continue
        t = _node_type(n)
        cfg = dict((n.get("data") or {}).get("config") or {})
        if t == "select_data_source":
            cfg.pop("aoi_id", None)                       # full item
        elif t == "run_inference":
            if cfg.get("model_id"):
                cfg["model_id"] = clone_model(cur, cfg["model_id"], model_map)
            if cfg.get("annotation_set_name"):
                cfg["annotation_set_name"] += "_bench"
        n = json.loads(json.dumps(n))                     # deep copy
        n.setdefault("data", {})["config"] = cfg
        new_nodes.append(n)

    keep_ids = {n["id"] for n in new_nodes}
    new_edges = []
    for e in edges:
        if e["source"] in drop or e["target"] in drop:
            continue
        if e.get("targetHandle") == "aoi":                # drop optional AOI input edges
            continue
        if e["source"] in keep_ids and e["target"] in keep_ids:
            new_edges.append(e)
    return {"nodes": new_nodes, "edges": new_edges}


def main():
    conn = psycopg2.connect(**DSN)
    conn.autocommit = False
    cur = conn.cursor()
    model_map: dict[str, str] = {}

    for task, src_name in SOURCES.items():
        new_name = src_name + SUFFIX
        cur.execute("SELECT id FROM automation_pipelines WHERE name=%s AND deleted_at IS NULL",
                    (new_name,))
        if cur.fetchone():
            print(f"[{task}] '{new_name}' already exists — skipping")
            continue

        cur.execute(
            "SELECT organization_id, project_id, description, trigger_type, trigger_config, "
            "graph, created_by FROM automation_pipelines WHERE name=%s AND deleted_at IS NULL",
            (src_name,))
        org, proj, desc, ttype, tcfg, graph, created_by = cur.fetchone()
        print(f"[{task}] cloning '{src_name}' -> '{new_name}'")

        new_graph = transform_graph(graph, task, cur, model_map)
        new_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO automation_pipelines
              (id, organization_id, project_id, name, description, trigger_type,
               trigger_config, graph, status, node_count, created_by, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,now(),now())
            """,
            (new_id, org, proj, new_name, (desc or "") + " [full-extent benchmark clone]",
             ttype, json.dumps(tcfg) if tcfg else None, json.dumps(new_graph),
             len(new_graph["nodes"]), created_by),
        )
        print(f"    -> pipeline {new_id}  ({len(new_graph['nodes'])} nodes, "
              f"{len(new_graph['edges'])} edges)")

    conn.commit()
    print("\nDone. New pipelines:")
    cur.execute("SELECT name, id, node_count FROM automation_pipelines "
                "WHERE name LIKE %s AND deleted_at IS NULL ORDER BY name", ("%" + SUFFIX,))
    for name, pid, nc in cur.fetchall():
        print(f"  {pid}  {nc:2d} nodes  {name}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
