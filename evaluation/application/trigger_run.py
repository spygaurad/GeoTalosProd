"""
Autonomous pipeline trigger — runs INSIDE the worker container (has the Celery
broker + a BYPASSRLS sync session), so it needs no Clerk auth or UI click.

Mirrors POST /pipelines/{id}/run exactly: create_run_sync -> dispatch root steps.
Prints the new run id on stdout (the host harness reads it).

  docker exec awakeforest-worker-inference \
      python -m evaluation.application.trigger_run --pipeline "<name>"
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.workers.db import WorkerSession
from app.models.automation import AutomationPipeline, AutomationRunStep
from app.automation.engine import create_run_sync, get_root_node_ids
from app.schemas.automation import ReactFlowGraph
from app.workers.automation.tasks import execute_step


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", required=True, help="exact pipeline name")
    args = ap.parse_args()

    with WorkerSession() as session:
        pipeline = session.execute(
            select(AutomationPipeline).where(
                AutomationPipeline.name == args.pipeline,
                AutomationPipeline.deleted_at.is_(None),
            )
        ).scalar_one()

        run = create_run_sync(session, pipeline, pipeline.trigger_type, None, None)
        session.flush()

        graph = ReactFlowGraph(**pipeline.graph)
        root_ids = get_root_node_ids(graph)
        root_steps = session.execute(
            select(AutomationRunStep).where(
                AutomationRunStep.run_id == run.id,
                AutomationRunStep.node_id.in_(root_ids),
            )
        ).scalars().all()

        run_id = str(run.id)
        session.commit()

        for step in root_steps:
            execute_step.delay(run_id, str(step.id))

    print(run_id)


if __name__ == "__main__":
    main()
