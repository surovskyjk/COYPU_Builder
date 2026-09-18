"""networkx-backed graph view over a `Network`, plus structural validation.

A `Network` is the source of truth; `build_graph` is a disposable projection of it, rebuilt on every call
so nothing here can drift from the entities it was built from.
"""

from __future__ import annotations

import networkx as nx

from coypu_builder.domain.model.ids import EntityId
from coypu_builder.domain.model.network import JunctionKind, Link, Network

_MIN_JUNCTION_DEGREE: dict[JunctionKind, int] = {
    JunctionKind.TURNOUT: 3,
    JunctionKind.SLIP: 3,
    JunctionKind.CROSSING: 3,
    JunctionKind.INTERSECTION: 3,
}


def build_graph(network: Network) -> nx.MultiGraph:
    """Undirected multigraph: nodes keyed by EntityId, edges carrying their `Link` (and its length as
    `weight`) so that a node pair joined by more than one link — e.g. a passing loop — keeps both edges.
    """
    graph = nx.MultiGraph()
    for node in network.nodes:
        graph.add_node(node.id, node=node)
    for link in network.links:
        graph.add_edge(
            link.start_node, link.end_node, key=link.id, link=link, weight=abs(link.s_end - link.s_start)
        )
    return graph


def connected_components(network: Network) -> list[set[EntityId]]:
    graph = build_graph(network)
    return [set(component) for component in nx.connected_components(graph)]


def shortest_path_links(network: Network, start: EntityId, end: EntityId) -> list[Link]:
    """The minimum-total-length chain of links from `start` to `end`, in traversal order."""
    graph = build_graph(network)
    node_path = nx.shortest_path(graph, start, end, weight="weight")
    links = []
    for a, b in zip(node_path[:-1], node_path[1:], strict=True):
        edges = graph.get_edge_data(a, b)
        best_key = min(edges, key=lambda k: edges[k]["weight"])
        links.append(edges[best_key]["link"])
    return links


def degree(network: Network, node_id: EntityId) -> int:
    graph = build_graph(network)
    return graph.degree(node_id) if node_id in graph else 0


def validate(network: Network) -> list[str]:
    """Structural warnings; never raises. Checks: dangling link endpoints, duplicate entity ids, junction
    kind inconsistent with node degree, zero-length links.

    A link's station range leaving its alignment's extent cannot be checked here — `Network` carries only
    `alignment_id`, not the `Alignment` geometry itself; that check belongs to a caller holding both.
    """
    warnings: list[str] = []
    seen_ids: set[EntityId] = set()
    node_ids = {node.id for node in network.nodes}

    for node in network.nodes:
        if node.id in seen_ids:
            warnings.append(f"duplicate entity id: node {node.id}")
        seen_ids.add(node.id)

    for link in network.links:
        if link.id in seen_ids:
            warnings.append(f"duplicate entity id: link {link.id}")
        seen_ids.add(link.id)
        if link.start_node not in node_ids:
            warnings.append(f"link {link.id} has a dangling start node {link.start_node}")
        if link.end_node not in node_ids:
            warnings.append(f"link {link.id} has a dangling end node {link.end_node}")
        if link.s_start == link.s_end:
            warnings.append(f"link {link.id} has a zero-length station range")

    graph = build_graph(network)
    for node in network.nodes:
        deg = graph.degree(node.id) if node.id in graph else 0
        if node.junction is None:
            if deg >= 3:
                warnings.append(f"node {node.id} has degree {deg} but no junction is declared")
            continue
        kind = node.junction.kind
        if kind is JunctionKind.BUFFER_STOP and deg != 1:
            warnings.append(f"node {node.id} is a {kind} but has degree {deg}, expected 1")
        elif kind is JunctionKind.CONNECTION and deg != 2:
            warnings.append(f"node {node.id} is a {kind} but has degree {deg}, expected 2")
        elif kind in _MIN_JUNCTION_DEGREE and deg < _MIN_JUNCTION_DEGREE[kind]:
            warnings.append(
                f"node {node.id} is a {kind} but has degree {deg}, expected at least "
                f"{_MIN_JUNCTION_DEGREE[kind]}"
            )
    return warnings
