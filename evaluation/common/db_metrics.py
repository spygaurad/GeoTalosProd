"""
Read-only metric extraction from the app's Postgres.

Honours "use DB timestamps, no tracking added in code": every timing number here
already exists in `automation_runs` / `automation_run_steps`. We only SELECT.

Uses `docker exec <db> psql` so the harness needs no Python pg driver.
"""
from __future__ import annotations

import subprocess
from typing import Any

try:
    from evaluation.task_spec import INFRA
except ImportError:  # allow running as a loose script
    from task_spec import INFRA  # type: ignore

_SEP = "\x1f"  # unit separator, safe against commas/spaces in values


def _psql(sql: str) -> list[list[str]]:
    """Run a SELECT and return rows as lists of string cells."""
    out = subprocess.run(
        ["docker", "exec", INFRA["db_container"],
         "psql", "-U", INFRA["db_user"], "-d", INFRA["db_name"],
         "-t", "-A", "-F", _SEP, "-c", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        if line.strip() == "":
            continue
        rows.append(line.split(_SEP))
    return rows


def get_pipeline_id(pipeline_name: str) -> str | None:
    sql = (f"SELECT id FROM automation_pipelines "
           f"WHERE name = $${pipeline_name}$$ AND deleted_at IS NULL LIMIT 1;")
    rows = _psql(sql)
    return rows[0][0] if rows else None


def latest_run_id(pipeline_name: str) -> str | None:
    """Most recent run id for a pipeline (used to detect a NEW run in watch mode)."""
    sql = (f"SELECT r.id FROM automation_runs r "
           f"JOIN automation_pipelines p ON p.id = r.pipeline_id "
           f"WHERE p.name = $${pipeline_name}$$ "
           f"ORDER BY r.created_at DESC LIMIT 1;")
    rows = _psql(sql)
    return rows[0][0] if rows else None


def run_status(run_id: str) -> dict[str, Any] | None:
    sql = (f"SELECT status, "
           f"to_char(started_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.MSOF'), "
           f"to_char(completed_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.MSOF'), "
           f"COALESCE(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000, 0) "
           f"FROM automation_runs WHERE id = '{run_id}';")
    rows = _psql(sql)
    if not rows:
        return None
    status, started, completed, e2e_ms = rows[0]
    return {
        "run_id": run_id,
        "status": status,
        "started_at": started or None,
        "completed_at": completed or None,
        "end_to_end_ms": round(float(e2e_ms)) if e2e_ms else 0,
    }


def compute_metrics(run_id: str) -> dict[str, Any]:
    """
    Derive all DB-sourced benchmark metrics for a finished run.

    Returns timing for both Table 1 (compute-comparison) and Table 2 (queueing-ablation):
      end_to_end_ms        run wall clock (run.completed - run.started)
      dataset_load_ms      Σ duration of select_data_source steps
      model_time_ms        Σ duration of run_inference steps
      model_runs           count of run_inference steps
      repeated_data_loads  count of select_data_source steps  (cache: platform reuses)
      worker_blocked_ms    Σ duration of ALL steps (time a worker was busy)
      per_node             {node_type: {count, total_ms}}
    """
    run = run_status(run_id) or {"end_to_end_ms": 0, "status": "unknown"}

    sql = (f"SELECT node_type, COUNT(*), COALESCE(SUM(duration_ms), 0) "
           f"FROM automation_run_steps "
           f"WHERE run_id = '{run_id}' AND status = 'completed' "
           f"GROUP BY node_type;")
    per_node: dict[str, dict[str, int]] = {}
    for node_type, count, total_ms in _psql(sql):
        per_node[node_type] = {"count": int(count), "total_ms": int(total_ms)}

    def _sum(nt: str) -> int:
        return per_node.get(nt, {}).get("total_ms", 0)

    def _cnt(nt: str) -> int:
        return per_node.get(nt, {}).get("count", 0)

    return {
        "run_id": run_id,
        "status": run["status"],
        "end_to_end_ms": run["end_to_end_ms"],
        "dataset_load_ms": _sum("select_data_source"),
        "model_time_ms": _sum("run_inference"),
        "model_runs": _cnt("run_inference"),
        "repeated_data_loads": _cnt("select_data_source"),
        "worker_blocked_ms": sum(v["total_ms"] for v in per_node.values()),
        "per_node": per_node,
    }
