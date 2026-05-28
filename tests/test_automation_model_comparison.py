from types import SimpleNamespace
from uuid import uuid4

from app.automation.engine import resolve_step_inputs, validate_graph
from app.automation.nodes.analysis import execute_aggregate_model_runs
from app.automation.nodes.iou_quality import execute_multi_model_iou_comparison
from app.models.ai_model import AIModel
from app.models.job import Job
from app.schemas.automation import ReactFlowEdge, ReactFlowGraph, ReactFlowNode


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _SessionStub:
    def __init__(self, *, jobs=None, models=None, annotation_set_rows=None):
        self._jobs = jobs or {}
        self._models = models or {}
        self._annotation_set_rows = annotation_set_rows or []

    def get(self, model_cls, key):
        if model_cls is Job:
            return self._jobs.get(str(key))
        if model_cls is AIModel:
            return self._models.get(str(key))
        return None

    def execute(self, *_args, **_kwargs):
        return _ScalarResult(self._annotation_set_rows)


def test_resolve_step_inputs_collects_multiple_values():
    graph = ReactFlowGraph(
        nodes=[
            ReactFlowNode(id="a", type="source", position={"x": 0, "y": 0}, data={"type": "select_model"}),
            ReactFlowNode(id="b", type="source", position={"x": 0, "y": 0}, data={"type": "select_model"}),
            ReactFlowNode(id="c", type="target", position={"x": 0, "y": 0}, data={"type": "aggregate_model_runs"}),
        ],
        edges=[
            ReactFlowEdge(id="e1", source="a", sourceHandle="model", target="c", targetHandle="predictions"),
            ReactFlowEdge(id="e2", source="b", sourceHandle="model", target="c", targetHandle="predictions"),
        ],
    )
    completed_steps = {
        "a": SimpleNamespace(output_data={"model": {"id": "m1"}}),
        "b": SimpleNamespace(output_data={"model": {"id": "m2"}}),
    }

    inputs = resolve_step_inputs(graph, "c", completed_steps)
    assert inputs == {"predictions": [{"id": "m1"}, {"id": "m2"}]}


def test_validate_graph_rejects_multiple_edges_on_single_input():
    graph = ReactFlowGraph(
        nodes=[
            ReactFlowNode(id="a", type="source", position={"x": 0, "y": 0}, data={"type": "select_model"}),
            ReactFlowNode(id="b", type="source", position={"x": 0, "y": 0}, data={"type": "select_model"}),
            ReactFlowNode(id="c", type="target", position={"x": 0, "y": 0}, data={"type": "run_inference"}),
        ],
        edges=[
            ReactFlowEdge(id="e1", source="a", sourceHandle="model", target="c", targetHandle="model"),
            ReactFlowEdge(id="e2", source="b", sourceHandle="model", target="c", targetHandle="model"),
        ],
    )

    result = validate_graph(graph)
    assert result.valid is False
    assert any(error.error_type == "multiple_inputs_not_allowed" for error in result.errors)


def test_aggregate_model_runs_returns_single_summary():
    model_id = str(uuid4())
    second_model_id = str(uuid4())
    first_job_id = str(uuid4())
    second_job_id = str(uuid4())
    session = _SessionStub(
        jobs={
            first_job_id: SimpleNamespace(model_id=model_id),
            second_job_id: SimpleNamespace(model_id=second_model_id),
        },
        models={
            model_id: SimpleNamespace(name="Model A"),
            second_model_id: SimpleNamespace(name="Model B"),
        },
    )

    result = execute_aggregate_model_runs(
        session,
        {},
        {
            "predictions": [
                {"job_id": first_job_id, "annotation_set_ids": ["s1"], "processed_items": 2, "failed_items": 0},
                {"job_id": second_job_id, "annotation_set_ids": ["s2", "s3"], "processed_items": 2, "failed_items": 1},
            ]
        },
    )

    summary = result["summary"]
    assert summary["model_run_count"] == 2
    assert summary["total_annotation_set_count"] == 3
    assert summary["total_processed_items"] == 4
    assert summary["total_failed_items"] == 1
    assert summary["model_runs"][0]["model_name"] == "Model A"


def test_multi_model_iou_comparison_builds_pairwise_summary(monkeypatch):
    first_model_id = str(uuid4())
    second_model_id = str(uuid4())
    first_job_id = str(uuid4())
    second_job_id = str(uuid4())
    item_id = str(uuid4())
    session = _SessionStub(
        jobs={
            first_job_id: SimpleNamespace(model_id=first_model_id),
            second_job_id: SimpleNamespace(model_id=second_model_id),
        },
        models={
            first_model_id: SimpleNamespace(name="Model A"),
            second_model_id: SimpleNamespace(name="Model B"),
        },
        annotation_set_rows=[
            SimpleNamespace(id=uuid4(), dataset_item_id=item_id),
            SimpleNamespace(id=uuid4(), dataset_item_id=item_id),
        ],
    )

    monkeypatch.setattr(
        "app.automation.nodes.iou_quality._compute_set_iou_summary",
        lambda *_a, **_kw: {
            "pairs": [],
            "summary": {
                "true_positives": 1,
                "false_positives": 0,
                "false_negatives": 0,
                "precision": 1.0,
                "recall": 1.0,
                "f1_score": 1.0,
                "mean_iou": 0.8,
                "matched_pairs": 1,
                "prediction_count": 1,
                "ground_truth_count": 1,
            },
        },
    )

    result = execute_multi_model_iou_comparison(
        session,
        {"iou_threshold": 0.5, "match_labels": True},
        {
            "predictions": [
                {"job_id": first_job_id, "annotation_set_ids": [str(session._annotation_set_rows[0].id)]},
                {"job_id": second_job_id, "annotation_set_ids": [str(session._annotation_set_rows[1].id)]},
            ]
        },
    )

    comparison = result["comparison"]
    assert comparison["summary"]["model_run_count"] == 2
    assert comparison["summary"]["comparison_count"] == 1
    assert comparison["pairwise_comparisons"][0]["mean_iou"] == 0.8
    assert comparison["pairwise_comparisons"][0]["left_model_name"] == "Model A"
