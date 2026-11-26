"""Shared pieces of the frozen implementation and delivery graphs."""
from ..orchestration.models import GraphDefinition, NodeDefinition, NodeType


LEGACY_DELIVERY_GRAPH_IDS = frozenset({
    "validated-delivery.v1", "validated-delivery-single.v1", "task-analysis-delivery.v1",
    "task-analysis-delivery-locked.v1", "task-analysis-delivery-single-locked.v1",
})
ORDINARY_ANALYSIS_DELIVERY_IDS = frozenset(f"task-analysis-delivery.v{v}" for v in (2, 3, 4))
MULTI_AGENT_DELIVERY_IDS = frozenset(f"validated-delivery.v{v}" for v in (2, 3, 4))
SINGLE_AGENT_DELIVERY_IDS = frozenset(f"validated-delivery-single.v{v}" for v in (2, 3, 4))
# Locked analysis graphs retain their v2 ID even with a v3/v4 graph version.
LOCKED_ANALYSIS_DELIVERY_IDS = frozenset({
    "task-analysis-delivery-locked.v2", "task-analysis-delivery-single-locked.v2",
})


def execution_approval(**settings) -> NodeDefinition:
    return NodeDefinition("execution_approval", NodeType.HUMAN_GATE, "execution_approval",
                          settings={"checkpoint_type": "plan_approved", **settings})


def analysis_with_approval(analysis: GraphDefinition, gate: NodeDefinition) -> tuple[NodeDefinition, ...]:
    """Join the fragments at their shared gate, keeping the execution settings."""
    analysis_gate = analysis.node(gate.node_id)
    return (
        *(node for node in analysis.nodes if node.node_id != gate.node_id),
        gate.with_dependencies(analysis_gate.dependencies),
    )
