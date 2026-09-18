# Entity model and topology (ADR 0006)

`backend/src/coypu_builder/domain/model/` and `domain/topology/`. Pure domain types: frozen dataclasses,
`EntityId` strings, no I/O, no protocol types, `networkx` for the graph view only.

## Identity

`domain/model/ids.py`:

```python
EntityId = str          # uuid4().hex — 32 lowercase hex characters
def new_id() -> EntityId: ...
```

ADR 0005 maps `EntityId` to an IFC `GlobalId` via `ifcopenshell.guid.compress` at export time, in `io/`
only — `ifcopenshell` is never imported from `domain/`.

## Placement: `LrsAnchor`

`domain/model/anchor.py`. The universal placement for every positioned entity — assets, and (once T-112
lands) trainsets on a run — is an offset into an alignment's linear reference system:

```python
@dataclass(frozen=True, slots=True)
class LrsAnchor:
    alignment_id: EntityId
    s: float                    # absolute station, metres (domain/lrs.py convention)
    y: float = 0.0               # left-positive lateral offset in the track plane
    z: float = 0.0               # offset along the track-plane normal ("up")
    heading_offset: float = 0.0  # radians, ccw, relative to the alignment tangent
```

`y`, `z` and the station convention match `docs/data-contracts/coordinate-conventions.md`. Nothing here
is mode-specific; `domain/lrs.py: to_xyz` resolves an anchor to `(E, N, H)` for any `Mode`.

## Network and topology

`domain/model/network.py`:

```python
class JunctionKind(StrEnum):
    TURNOUT = "turnout"
    CROSSING = "crossing"
    SLIP = "slip"
    INTERSECTION = "intersection"     # road / tram street intersection
    BUFFER_STOP = "buffer_stop"
    CONNECTION = "connection"         # plain end-to-end continuity, no special semantics

@dataclass(frozen=True, slots=True)
class Junction:
    kind: JunctionKind
    properties: Mapping[str, object] = {}   # kind-specific (e.g. turnout hand/radius); generated
                                            # geometry is Phase 2 and is never modelled here

@dataclass(frozen=True, slots=True)
class Node:
    id: EntityId
    position: tuple[float, float, float]    # (E, N, H) float64, project CRS — denormalised from the
                                             # alignment(s) that meet here, for display and lookup only
    junction: Junction | None = None        # None = a plain topological node (a mid-alignment split)
    name: str = ""

@dataclass(frozen=True, slots=True)
class Link:
    id: EntityId
    alignment_id: EntityId
    start_node: EntityId
    end_node: EntityId
    s_start: float           # station range of the alignment this link occupies
    s_end: float              # may be less than s_start: the link is traversed against the
                              # alignment's station direction. Nothing here rejects that.
    name: str = ""

@dataclass(frozen=True, slots=True)
class Network:
    id: EntityId
    mode: Mode
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]
    name: str = ""
```

A `Network` is one mode's topology; a project composed of multiple modes (e.g. heavy rail plus a tram
network sharing an alignment corridor) holds one `Network` per mode, not one mixed graph — `mode` is a
field of `Network`, not of `Node`/`Link`.

### `domain/topology/graph.py`

Builds a disposable `networkx.MultiGraph` view over a `Network` on every call — the `Network` is the
source of truth, the graph is never persisted or mutated in place. A `MultiGraph` because a node pair may
be joined by more than one `Link` (a passing loop, or — as in the tram fixture — the two halves of a
terminal loop).

```python
def build_graph(network: Network) -> nx.MultiGraph: ...
def connected_components(network: Network) -> list[set[EntityId]]: ...
def shortest_path_links(network: Network, start: EntityId, end: EntityId) -> list[Link]: ...
def degree(network: Network, node_id: EntityId) -> int: ...
def validate(network: Network) -> list[str]: ...
```

`shortest_path_links` minimises total `|s_end - s_start|` across the path and returns the `Link`s in
traversal order; it raises `networkx.NetworkXNoPath` (not caught) when the two nodes are disconnected.

`validate` never raises — it returns human-readable warnings for:

- a link endpoint (`start_node`/`end_node`) that is not one of the network's node ids ("dangling")
- a duplicate `EntityId` shared by two nodes, two links, or a node and a link
- a link with `s_start == s_end` (zero-length station range)
- a node degree inconsistent with its declared `JunctionKind`: `BUFFER_STOP` must be degree 1,
  `CONNECTION` must be degree 2, `TURNOUT`/`SLIP`/`CROSSING`/`INTERSECTION` must be degree ≥ 3, and a
  node with **no** `Junction` but degree ≥ 3 is flagged (a real turnout that nobody annotated)

It does **not** check whether a link's station range falls inside its alignment's extent — `Network`
carries only `alignment_id`, never the `Alignment` geometry, so that check needs a caller holding both and
belongs there, not in `domain/topology`.

## Corridor and cross-section (declared only)

`domain/model/corridor.py`. Types only — no sweeping, no section library. That is Phase 2
(`domain/sections/` stays empty; roadmap decision 3).

```python
@dataclass(frozen=True, slots=True)
class CrossSectionInterval:
    s_start: float
    s_end: float
    section_id: str                        # catalogue key; the catalogue itself is Phase 2
    parameters: Mapping[str, float] = {}

@dataclass(frozen=True, slots=True)
class Corridor:
    id: EntityId
    alignment_id: EntityId
    intervals: tuple[CrossSectionInterval, ...] = ()
    name: str = ""
```

## Asset

`domain/model/asset.py`. A catalogue-backed item placed by `LrsAnchor` — a signal, a pole, a switch
machine — independent of `Mode`:

```python
@dataclass(frozen=True, slots=True)
class Asset:
    id: EntityId
    catalogue_id: str
    anchor: LrsAnchor
    layer_id: EntityId | None = None
    properties: Mapping[str, object] = {}
    name: str = ""
```

## Organisation: `Layer`, `WbsNode`

`domain/model/organisation.py`. Two independent trees over the same entities — display grouping and cost
breakdown — neither owning the entities it groups:

```python
@dataclass(frozen=True, slots=True)
class Layer:
    id: EntityId
    name: str
    visible: bool = True
    opacity: float = 1.0
    parent_id: EntityId | None = None

@dataclass(frozen=True, slots=True)
class WbsNode:
    id: EntityId
    code: str
    name: str
    parent_id: EntityId | None = None
```

## Not defined here

`Trainset` and `KinematicsRun` (ADR 0006 names them, but they are T-113 and T-111 respectively, kept out
so those tasks can proceed in parallel) — not even as stubs.

## Synthetic tram fixture

`backend/tests/fixtures/synthetic/tram_loop.py: build_tram_loop() -> tuple[Network, dict[EntityId,
Alignment]]`. Built entirely in code (no data files): a street `Alignment` feeding a closed terminal-loop
`Alignment`, `Mode.LIGHT_RAIL_TRAM`, 1000 mm gauge with a 1100 mm superelevation base. Four nodes
(`terminus`: `BUFFER_STOP`, degree 1; `street_mid`: plain, degree 2; `loop_entry`: `TURNOUT`, degree 3;
`loop_far_side`: plain, degree 2) and four links; `validate()` returns `[]` for it. Exists to prove the
non-heavy-rail requirement in ADR 0006's consequences section — see `backend/tests/test_multimodal.py`.
