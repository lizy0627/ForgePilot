"""Node execution interfaces and deterministic fan-out/join boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, TypeAlias

from ..artifacts.store import ArtifactStore
from .models import FailureKind, NodeDefinition, NodeResult, NodeStatus, NodeType


@dataclass(frozen=True, slots=True)
class NodeContext:
    task_id: str
    node: NodeDefinition
    attempt: int
    input_artifact_ids: tuple[str, ...]
    input_artifact_hashes: tuple[str, ...]
    artifacts: ArtifactStore
    is_cancelled: Callable[[], bool]
    checkpoint_id: str | None = None


class NodeHandler(Protocol):
    def __call__(self, context: NodeContext) -> NodeResult: ...


# Named aliases document the stable interface for each executable node kind.
AgentNodeHandler: TypeAlias = NodeHandler
DeterministicNodeHandler: TypeAlias = NodeHandler
HumanGateNodeHandler: TypeAlias = NodeHandler
ConditionalNodeHandler: TypeAlias = NodeHandler
CheckpointNodeHandler: TypeAlias = NodeHandler
DeliveryNodeHandler: TypeAlias = NodeHandler


class NodeExecutionError(RuntimeError):
    def __init__(self, failure_kind: FailureKind, summary: str) -> None:
        self.failure_kind = FailureKind(failure_kind)
        self.summary = summary[:8_192]
        super().__init__(self.summary)


class ArtifactBoundaryHandler:
    """Forward immutable inputs; scheduling and all-success joining live in the runner."""

    def __call__(self, context: NodeContext) -> NodeResult:
        return NodeResult(
            NodeStatus.SUCCEEDED, output_artifact_ids=context.input_artifact_ids,
            decision_summary=f"{context.node.node_type.value} Artifact boundary completed",
        )


class HandlerRegistry:
    def __init__(self, handlers: Mapping[str, NodeHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})

    def register(self, handler_id: str, handler: NodeHandler) -> None:
        if handler_id in self._handlers:
            raise ValueError(f"Handler is already registered: {handler_id}")
        self._handlers[handler_id] = handler

    def resolve(self, node: NodeDefinition) -> NodeHandler:
        if node.handler in self._handlers:
            return self._handlers[node.handler]
        if node.node_type in {NodeType.PARALLEL_FANOUT, NodeType.JOIN}:
            return ArtifactBoundaryHandler()
        try:
            return self._handlers[node.handler]
        except KeyError:
            raise NodeExecutionError(
                FailureKind.TOOL_POLICY_VIOLATION,
                f"No handler registered for node {node.node_id}",
            ) from None


__all__ = [
    "AgentNodeHandler", "CheckpointNodeHandler", "ConditionalNodeHandler",
    "DeliveryNodeHandler", "DeterministicNodeHandler", "HandlerRegistry",
    "HumanGateNodeHandler", "NodeContext", "NodeExecutionError", "NodeHandler",
    "ArtifactBoundaryHandler",
]
