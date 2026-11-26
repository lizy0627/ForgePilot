"""Immutable graph protocol, state machine, results, and event projections."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RETRYABLE = "retryable"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class NodeType(str, Enum):
    AGENT = "agent"
    DETERMINISTIC = "deterministic"
    PARALLEL_FANOUT = "parallel_fanout"
    JOIN = "join"
    HUMAN_GATE = "human_gate"
    CONDITIONAL = "conditional"
    CHECKPOINT = "checkpoint"
    DELIVERY = "delivery"


class FailureKind(str, Enum):
    MODEL_TRANSIENT = "model_transient"
    MODEL_RATE_LIMIT = "model_rate_limit"
    MODEL_CONTRACT_VIOLATION = "model_contract_violation"
    TOOL_TRANSIENT = "tool_transient"
    TOOL_POLICY_VIOLATION = "tool_policy_violation"
    REPOSITORY_CONFLICT = "repository_conflict"
    WORKSPACE_CORRUPTED = "workspace_corrupted"
    BUILD_CONFIGURATION = "build_configuration"
    BUILD_CODE_ERROR = "build_code_error"
    TEST_FAILURE = "test_failure"
    TEST_INTEGRITY_VIOLATION = "test_integrity_violation"
    HIDDEN_ORACLE_FAILURE = "hidden_oracle_failure"
    STORAGE_FAILURE = "storage_failure"
    USER_CANCELLED = "user_cancelled"
    APPLICATION_INTERRUPTED = "application_interrupted"


class PolicyAction(str, Enum):
    RETRY = "retry"
    REPAIR = "repair"
    PAUSE = "pause"
    STOP = "stop"


TERMINAL_NODE_STATUSES = frozenset({
    NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.CANCELLED,
})

# This is the one authoritative state machine. Persistence calls validate it too.
ALLOWED_TRANSITIONS: Mapping[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.READY, NodeStatus.SKIPPED, NodeStatus.CANCELLED}),
    NodeStatus.READY: frozenset({NodeStatus.RUNNING, NodeStatus.SKIPPED, NodeStatus.CANCELLED}),
    NodeStatus.RUNNING: frozenset({
        NodeStatus.AWAITING_HUMAN, NodeStatus.SUCCEEDED, NodeStatus.FAILED,
        NodeStatus.INTERRUPTED, NodeStatus.RETRYABLE, NodeStatus.CANCELLED,
    }),
    NodeStatus.AWAITING_HUMAN: frozenset({
        NodeStatus.RUNNING, NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED,
    }),
    NodeStatus.SUCCEEDED: frozenset(),
    NodeStatus.FAILED: frozenset(),
    NodeStatus.INTERRUPTED: frozenset({NodeStatus.RETRYABLE, NodeStatus.CANCELLED}),
    NodeStatus.RETRYABLE: frozenset({NodeStatus.READY, NodeStatus.CANCELLED}),
    NodeStatus.SKIPPED: frozenset(),
    NodeStatus.CANCELLED: frozenset(),
}


class TransitionError(ValueError):
    pass


def assert_transition(source: NodeStatus | str, target: NodeStatus | str) -> None:
    try:
        before, after = NodeStatus(source), NodeStatus(target)
    except ValueError:
        raise TransitionError("Unknown node state") from None
    if after not in ALLOWED_TRANSITIONS[before]:
        raise TransitionError(f"Illegal node transition: {before.value} -> {after.value}")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class NodeMetrics:
    duration_ms: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (
                self.duration_ms, self.model_calls, self.tool_calls, self.total_tokens)):
            raise ValueError("Node metrics must be non-negative integers")


@dataclass(frozen=True, slots=True)
class NodeResult:
    status: NodeStatus
    output_artifact_ids: tuple[str, ...] = ()
    decision_summary: str = ""
    next_route: str | None = None
    retryable: bool = False
    metrics: NodeMetrics = field(default_factory=NodeMetrics)
    failure_kind: FailureKind | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", NodeStatus(self.status))
        if self.status not in {
                NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.RETRYABLE,
                NodeStatus.AWAITING_HUMAN, NodeStatus.CANCELLED, NodeStatus.INTERRUPTED}:
            raise ValueError("NodeResult status is not executable output")
        if type(self.retryable) is not bool:
            raise ValueError("retryable must be bool")
        if len(self.decision_summary) > 8_192 or (self.error_summary and len(self.error_summary) > 8_192):
            raise ValueError("NodeResult summaries are too large")
        if len(set(self.output_artifact_ids)) != len(self.output_artifact_ids):
            raise ValueError("NodeResult contains duplicate Artifact IDs")
        for artifact_id in self.output_artifact_ids:
            _identifier(artifact_id, "output Artifact ID")
        if self.next_route is not None:
            _identifier(self.next_route, "next route")
        if self.failure_kind is not None:
            object.__setattr__(self, "failure_kind", FailureKind(self.failure_kind))
        if self.status in {NodeStatus.FAILED, NodeStatus.RETRYABLE, NodeStatus.INTERRUPTED} \
                and self.failure_kind is None:
            raise ValueError("Failed, retryable, and interrupted results require failure_kind")

    def with_duration(self, duration_ms: int) -> "NodeResult":
        return NodeResult(
            status=self.status, output_artifact_ids=self.output_artifact_ids,
            decision_summary=self.decision_summary, next_route=self.next_route,
            retryable=self.retryable,
            metrics=NodeMetrics(duration_ms=duration_ms, model_calls=self.metrics.model_calls,
                                tool_calls=self.metrics.tool_calls,
                                total_tokens=self.metrics.total_tokens),
            failure_kind=self.failure_kind, error_summary=self.error_summary,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["failure_kind"] = self.failure_kind.value if self.failure_kind else None
        value["output_artifact_ids"] = list(self.output_artifact_ids)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeResult":
        data = dict(value)
        data["output_artifact_ids"] = tuple(data.get("output_artifact_ids", ()))
        data["metrics"] = NodeMetrics(**data.get("metrics", {}))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class EdgeCondition:
    """A deterministic route/status predicate over the source NodeResult."""

    route: str | None = None
    statuses: tuple[NodeStatus, ...] = (NodeStatus.SUCCEEDED,)

    def __post_init__(self) -> None:
        if self.route is not None:
            _identifier(self.route, "edge route")
        statuses = tuple(NodeStatus(status) for status in self.statuses)
        if not statuses or len(set(statuses)) != len(statuses):
            raise ValueError("Edge condition statuses must be unique and non-empty")
        object.__setattr__(self, "statuses", statuses)

    def matches(self, result: NodeResult | None, status: NodeStatus) -> bool:
        if status not in self.statuses:
            return False
        return self.route is None or (result is not None and result.next_route == self.route)


@dataclass(frozen=True, slots=True)
class EdgeDefinition:
    source: str
    target: str
    condition: EdgeCondition = field(default_factory=EdgeCondition)

    def __post_init__(self) -> None:
        _identifier(self.source, "edge source")
        _identifier(self.target, "edge target")


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    node_id: str
    node_type: NodeType
    handler: str
    dependencies: tuple[str, ...] = ()
    agent_id: str | None = None
    agent_definition_hash: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node ID")
        object.__setattr__(self, "node_type", NodeType(self.node_type))
        _identifier(self.handler, "handler ID")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Node dependencies must be unique")
        for dependency in self.dependencies:
            _identifier(dependency, "dependency ID")
        if self.agent_id is not None:
            _identifier(self.agent_id, "agent ID")
        if self.agent_definition_hash is not None and not SHA256.fullmatch(self.agent_definition_hash):
            raise ValueError("agent_definition_hash must be SHA256")
        try:
            frozen_settings = json.loads(_canonical(dict(self.settings)))
        except (TypeError, ValueError):
            raise ValueError("Node settings must be canonical JSON") from None
        object.__setattr__(self, "settings", _freeze_json(frozen_settings))

    def with_dependencies(self, dependencies: tuple[str, ...]) -> "NodeDefinition":
        return replace(self, dependencies=dependencies, settings=_plain_json(self.settings))

    @property
    def definition_hash(self) -> str:
        return sha256(_canonical({
            "node_id": self.node_id, "node_type": self.node_type.value,
            "handler": self.handler, "dependencies": list(self.dependencies),
            "agent_id": self.agent_id, "settings": _plain_json(self.settings),
        })).hexdigest()

    @property
    def effective_agent_definition_hash(self) -> str:
        return self.agent_definition_hash or self.definition_hash


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    graph_id: str
    version: int
    nodes: tuple[NodeDefinition, ...]
    edges: tuple[EdgeDefinition, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.graph_id, "graph ID")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Graph version must be a positive integer")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))

    def node(self, node_id: str) -> NodeDefinition:
        return next(node for node in self.nodes if node.node_id == node_id)

    def to_dict(self) -> dict[str, Any]:
        return {"graph_id": self.graph_id, "version": self.version,
                "nodes": [{"node_id": node.node_id, "node_type": node.node_type.value,
                           "handler": node.handler, "dependencies": list(node.dependencies),
                           "agent_id": node.agent_id, "agent_definition_hash": node.agent_definition_hash,
                           "settings": _plain_json(node.settings)} for node in self.nodes],
                "edges": [{"source": edge.source, "target": edge.target,
                           "condition": {"route": edge.condition.route,
                                         "statuses": [status.value for status in edge.condition.statuses]}}
                          for edge in self.edges]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphDefinition":
        data = dict(value)
        data["nodes"] = tuple(NodeDefinition(**{**node, "dependencies": tuple(node.get("dependencies", ()))})
                              for node in data["nodes"])
        data["edges"] = tuple(EdgeDefinition(edge["source"], edge["target"],
                               EdgeCondition(**edge.get("condition", {}))) for edge in data.get("edges", ()))
        return cls(**data)

    @property
    def definition_hash(self) -> str:
        return sha256(_canonical({
            "graph_id": self.graph_id, "version": self.version,
            "nodes": [{
                "node_id": node.node_id, "node_type": node.node_type.value,
                "handler": node.handler, "dependencies": list(node.dependencies),
                "agent_id": node.agent_id,
                "agent_definition_hash": node.effective_agent_definition_hash,
                "settings": _plain_json(node.settings),
            } for node in self.nodes],
            "edges": [{
                "source": edge.source, "target": edge.target,
                "route": edge.condition.route,
                "statuses": [status.value for status in edge.condition.statuses],
            } for edge in self.edges],
        })).hexdigest()


@dataclass(frozen=True, slots=True)
class NodeState:
    node_id: str
    node_type: NodeType
    status: NodeStatus
    attempt: int
    result: NodeResult | None = None


@dataclass(frozen=True, slots=True)
class GraphEvent:
    task_id: str
    sequence: int
    revision: int
    event_type: str
    node_id: str | None
    attempt: int | None
    state: NodeStatus | None
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    task_id: str
    graph_id: str
    graph_version: int
    definition_hash: str
    status: NodeStatus
    revision: int
    nodes: tuple[NodeState, ...]


def idempotency_key(*, task_id: str, node_id: str, attempt: int,
                    input_artifact_hashes: tuple[str, ...], agent_definition_hash: str,
                    policy_hash: str) -> str:
    _identifier(task_id, "task ID")
    _identifier(node_id, "node ID")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Attempt must be a positive integer")
    hashes = tuple(sorted(input_artifact_hashes))
    if any(not SHA256.fullmatch(value) for value in (*hashes, agent_definition_hash, policy_hash)):
        raise ValueError("Idempotency inputs must be SHA256 hashes")
    return sha256(_canonical({
        "task_id": task_id, "node_id": node_id, "attempt": attempt,
        "input_artifact_hashes": hashes,
        "agent_definition_hash": agent_definition_hash, "policy_hash": policy_hash,
    })).hexdigest()


__all__ = [
    "ALLOWED_TRANSITIONS", "EdgeCondition", "EdgeDefinition", "FailureKind",
    "GraphDefinition", "GraphEvent", "GraphSnapshot", "NodeDefinition", "NodeMetrics",
    "NodeResult", "NodeState", "NodeStatus", "NodeType", "PolicyAction",
    "TERMINAL_NODE_STATUSES", "TransitionError", "assert_transition", "idempotency_key",
]
