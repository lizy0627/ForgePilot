"""Graph validation and stable topological planning."""
from __future__ import annotations

from dataclasses import dataclass

from .models import EdgeDefinition, GraphDefinition, NodeDefinition


class GraphValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedGraph:
    definition: GraphDefinition
    topological_order: tuple[str, ...]

    @property
    def nodes(self) -> dict[str, NodeDefinition]:
        return {node.node_id: node for node in self.definition.nodes}

    def incoming_edges(self, node_id: str) -> tuple[EdgeDefinition, ...]:
        return tuple(edge for edge in self.definition.edges if edge.target == node_id)

    def predecessors(self, node_id: str) -> tuple[str, ...]:
        node = self.nodes[node_id]
        incoming = [edge.source for edge in self.incoming_edges(node_id)]
        return tuple(dict.fromkeys((*node.dependencies, *incoming)))


def validate_graph(graph: GraphDefinition) -> ValidatedGraph:
    if not isinstance(graph, GraphDefinition):
        raise GraphValidationError("Expected GraphDefinition")
    identifiers = [node.node_id for node in graph.nodes]
    if not identifiers:
        raise GraphValidationError("Graph must contain at least one node")
    if len(set(identifiers)) != len(identifiers):
        raise GraphValidationError("Graph contains duplicate node IDs")
    known = set(identifiers)
    arcs: set[tuple[str, str]] = set()
    for node in graph.nodes:
        missing = set(node.dependencies) - known
        if missing:
            raise GraphValidationError(
                f"Node {node.node_id} has missing dependencies: {', '.join(sorted(missing))}")
        arcs.update((dependency, node.node_id) for dependency in node.dependencies)
    edge_keys = set()
    for edge in graph.edges:
        if edge.source not in known or edge.target not in known:
            raise GraphValidationError(
                f"Edge references missing node: {edge.source} -> {edge.target}")
        key = (edge.source, edge.target, edge.condition.route,
               tuple(status.value for status in edge.condition.statuses))
        if key in edge_keys:
            raise GraphValidationError("Graph contains a duplicate edge")
        edge_keys.add(key)
        arcs.add((edge.source, edge.target))
    if any(source == target for source, target in arcs):
        raise GraphValidationError("Graph contains a self-cycle")

    # Kahn with definition order as the tie breaker gives deterministic scheduling.
    rank = {node_id: index for index, node_id in enumerate(identifiers)}
    incoming_count = {node_id: 0 for node_id in identifiers}
    outgoing = {node_id: [] for node_id in identifiers}
    for source, target in arcs:
        incoming_count[target] += 1
        outgoing[source].append(target)
    ready = sorted((node_id for node_id, count in incoming_count.items() if count == 0),
                   key=rank.__getitem__)
    ordered: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for target in sorted(outgoing[node_id], key=rank.__getitem__):
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
                ready.sort(key=rank.__getitem__)
    if len(ordered) != len(identifiers):
        cyclic = sorted(node_id for node_id, count in incoming_count.items() if count)
        raise GraphValidationError("Graph contains a cycle involving: " + ", ".join(cyclic))
    return ValidatedGraph(graph, tuple(ordered))


__all__ = ["GraphValidationError", "ValidatedGraph", "validate_graph"]
