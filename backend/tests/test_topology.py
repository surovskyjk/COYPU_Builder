import dataclasses

import networkx as nx
import pytest

from coypu_builder.domain.model.ids import new_id
from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.model.network import Junction, JunctionKind, Link, Network, Node
from coypu_builder.domain.topology.graph import (
    build_graph,
    connected_components,
    degree,
    shortest_path_links,
    validate,
)


def _malformed_network() -> Network:
    n1, n2 = new_id(), new_id()
    turnout = Node(id=n2, position=(0.0, 0.0, 0.0), junction=Junction(JunctionKind.TURNOUT))
    duplicate = Node(id=n1, position=(20.0, 0.0, 0.0))
    nodes = (Node(id=n1, position=(0.0, 0.0, 0.0)), turnout, duplicate)
    links = (
        Link(id=new_id(), alignment_id=new_id(), start_node=n1, end_node=n2, s_start=0.0, s_end=10.0),
        Link(id=new_id(), alignment_id=new_id(), start_node=n2, end_node="missing", s_start=10.0, s_end=10.0),
    )
    return Network(id=new_id(), mode=Mode.HEAVY_RAIL, nodes=nodes, links=links)


def test_build_graph_is_a_multigraph_keyed_by_entity_id(tram_network):
    graph = build_graph(tram_network)
    assert isinstance(graph, nx.MultiGraph)
    assert set(graph.nodes) == {n.id for n in tram_network.nodes}
    for link in tram_network.links:
        assert graph.has_edge(link.start_node, link.end_node, key=link.id)
        assert graph[link.start_node][link.end_node][link.id]["link"] is link


def test_validate_flags_dangling_endpoint_duplicate_id_and_bad_junction_degree():
    warnings = validate(_malformed_network())
    joined = " | ".join(warnings)
    assert "dangling" in joined
    assert "duplicate entity id" in joined
    assert "turnout" in joined and "degree 2" in joined
    assert any("zero-length" in w for w in warnings)


def test_validate_is_clean_for_the_tram_fixture(tram_network):
    assert validate(tram_network) == []


def test_degree_matches_topological_role(tram_network):
    turnout = next(n for n in tram_network.nodes if n.junction and n.junction.kind is JunctionKind.TURNOUT)
    buffer_stop = next(
        n for n in tram_network.nodes if n.junction and n.junction.kind is JunctionKind.BUFFER_STOP
    )
    plain_nodes = [n for n in tram_network.nodes if n.junction is None]
    assert degree(tram_network, turnout.id) == 3
    assert degree(tram_network, buffer_stop.id) == 1
    assert all(degree(tram_network, n.id) == 2 for n in plain_nodes)


def test_shortest_path_links_crosses_the_turnout(tram_network):
    buffer_stop = next(
        n for n in tram_network.nodes if n.junction and n.junction.kind is JunctionKind.BUFFER_STOP
    )
    turnout = next(n for n in tram_network.nodes if n.junction and n.junction.kind is JunctionKind.TURNOUT)
    far_side = next(n for n in tram_network.nodes if n.name == "loop_far_side")

    path = shortest_path_links(tram_network, buffer_stop.id, far_side.id)
    touched = {link.start_node for link in path} | {link.end_node for link in path}
    assert turnout.id in touched
    assert path[0].start_node == buffer_stop.id
    assert path[-1].end_node == far_side.id


def test_connected_components_one_for_tram_fixture_two_after_removing_a_link(tram_network):
    assert len(connected_components(tram_network)) == 1

    disconnected = dataclasses.replace(tram_network, links=tram_network.links[1:])
    components = connected_components(disconnected)
    assert len(components) == 2
    assert {1, 3} == {len(c) for c in components}


def test_shortest_path_links_raises_when_no_path_exists(tram_network):
    disconnected = dataclasses.replace(tram_network, links=tram_network.links[1:])
    isolated = next(n for n in disconnected.nodes if degree(disconnected, n.id) == 0)
    other = next(n for n in disconnected.nodes if n.id != isolated.id)
    with pytest.raises(nx.NetworkXNoPath):
        shortest_path_links(disconnected, isolated.id, other.id)
