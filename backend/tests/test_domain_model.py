import dataclasses
import re

import pytest

from coypu_builder.domain.model import (
    Asset,
    Corridor,
    CrossSectionInterval,
    Junction,
    JunctionKind,
    Layer,
    Link,
    LrsAnchor,
    Mode,
    Network,
    Node,
    WbsNode,
    new_id,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def test_new_id_is_32_lowercase_hex_and_unique():
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(_HEX32.match(i) for i in ids)


def test_lrs_anchor_defaults():
    anchor = LrsAnchor(alignment_id=new_id(), s=42.0)
    assert anchor.y == 0.0
    assert anchor.z == 0.0
    assert anchor.heading_offset == 0.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        anchor.s = 1.0


def test_node_link_network_construction_and_immutability():
    a, b = new_id(), new_id()
    node_a = Node(id=a, position=(0.0, 0.0, 0.0), junction=Junction(JunctionKind.BUFFER_STOP))
    node_b = Node(id=b, position=(10.0, 0.0, 0.0), name="far end")
    link = Link(id=new_id(), alignment_id=new_id(), start_node=a, end_node=b, s_start=0.0, s_end=10.0)
    network = Network(id=new_id(), mode=Mode.LIGHT_RAIL_TRAM, nodes=(node_a, node_b), links=(link,))
    assert network.nodes == (node_a, node_b)
    assert network.links == (link,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        network.name = "renamed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        node_a.position = (1.0, 1.0, 1.0)


def test_link_station_range_may_descend():
    link = Link(
        id=new_id(), alignment_id=new_id(), start_node=new_id(), end_node=new_id(), s_start=50.0, s_end=10.0
    )
    assert link.s_start > link.s_end


def test_corridor_and_cross_section_interval_are_declared_only():
    interval = CrossSectionInterval(s_start=0.0, s_end=50.0, section_id="catalogue-key")
    assert interval.parameters == {}
    corridor = Corridor(id=new_id(), alignment_id=new_id(), intervals=(interval,))
    assert corridor.intervals == (interval,)
    assert Corridor(id=new_id(), alignment_id=new_id()).intervals == ()


def test_asset_carries_an_lrs_anchor():
    anchor = LrsAnchor(alignment_id=new_id(), s=12.5, y=1.5)
    asset = Asset(id=new_id(), catalogue_id="signal.type-a", anchor=anchor)
    assert asset.anchor is anchor
    assert asset.layer_id is None
    assert asset.properties == {}


def test_layer_and_wbs_node_parent_chains():
    root_layer = Layer(id=new_id(), name="root")
    child_layer = Layer(id=new_id(), name="child", parent_id=root_layer.id, visible=False, opacity=0.5)
    assert child_layer.parent_id == root_layer.id
    assert child_layer.visible is False

    root_wbs = WbsNode(id=new_id(), code="1", name="Project")
    child_wbs = WbsNode(id=new_id(), code="1.1", name="Alignment", parent_id=root_wbs.id)
    assert child_wbs.parent_id == root_wbs.id


def test_junction_kinds_cover_the_adr_vocabulary():
    assert {k.value for k in JunctionKind} == {
        "turnout",
        "crossing",
        "slip",
        "intersection",
        "buffer_stop",
        "connection",
    }
