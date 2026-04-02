from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class HandleDef:
    handle: str
    type: str
    required: bool = True
    multiple: bool = False
    label: str | None = None


class DeferToJob:
    """Returned by an executor to indicate it delegated work to a long-running Job.

    The execute_step task will set the step to 'waiting_for_job' instead of 'completed'.
    When the Job finishes, call resume_after_job() to continue the pipeline.
    """
    def __init__(self, job_id: str, output_data: dict[str, Any] | None = None):
        self.job_id = job_id
        self.output_data = output_data or {}


@dataclass
class NodeType:
    type: str
    category: str
    label: str
    description: str
    inputs: list[HandleDef] = field(default_factory=list)
    outputs: list[HandleDef] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    executor: Callable | None = None  # Direct reference to executor function
    icon: str | None = None
    color: str | None = None
    status: str = "implemented"  # "implemented" or "placeholder"
    frontend_preview: bool = False  # True if UI can compute a live preview client-side


# ─── Registry ─────────────────────────────────────────────────────────────

NODE_REGISTRY: Dict[str, NodeType] = {}


def register_node(node: NodeType) -> NodeType:
    """Register a node type in the global registry."""
    NODE_REGISTRY[node.type] = node
    return node


def get_node_type(node_type: str) -> NodeType | None:
    _ensure_nodes_loaded()
    return NODE_REGISTRY.get(node_type)


def get_catalog() -> Dict[str, List[NodeType]]:
    """Return node types grouped by category."""
    _ensure_nodes_loaded()
    by_category: Dict[str, List[NodeType]] = {}
    for node in NODE_REGISTRY.values():
        by_category.setdefault(node.category, []).append(node)
    return by_category


_nodes_loaded = False


def _ensure_nodes_loaded():
    """Lazily import all node modules so their @node decorators run."""
    global _nodes_loaded
    if _nodes_loaded:
        return
    import app.automation.nodes  # noqa: F401 — triggers all @node registrations
    _nodes_loaded = True


# ─── Decorator ────────────────────────────────────────────────────────────

def node(**kwargs):
    """Decorator that registers a function as a node executor."""
    def decorator(func):
        node_type = NodeType(
            type=kwargs.get("type", func.__name__),
            category=kwargs.get("category", "general"),
            label=kwargs.get("label", func.__name__.replace("_", " ").title()),
            description=kwargs.get("description", func.__doc__ or ""),
            inputs=kwargs.get("inputs", []),
            outputs=kwargs.get("outputs", []),
            config_schema=kwargs.get("config_schema", {}),
            executor=func,
            icon=kwargs.get("icon"),
            color=kwargs.get("color"),
            status=kwargs.get("status", "implemented"),
            frontend_preview=kwargs.get("frontend_preview", False),
        )
        register_node(node_type)
        return func
    return decorator
