# T-110 — ADR 0006 entity model, topology graph, synthetic tram fixture

**Milestone:** P1.M1 · **Retires debt:** D3 (part), D6 · **Depends on:** nothing · **Blocks:** T-112, T-114,
T-120, T-145

## Context

ADR 0006 names the domain entities — `Network(mode)`, `Node`, `Link`, `Junction`, `Alignment`, `Corridor`,
`Asset`, `Trainset`, `KinematicsRun`, `Layer`, `WbsNode` — and states that placement is always an LRS anchor
`(alignment, s, y, z, heading offset)`. Only `Alignment` and `Mode` exist. `domain/model/` is `modes.py`
alone; `domain/topology/` is empty.

ADR 0006 also carries a consequence that has gone unmet: *"Fixtures must include at least one non-heavy-rail
network (tram loop with a junction) before Phase 2 authoring ships; no rail-only assumption may live in
`domain/lrs.py` or `domain/model`."* Seeding that fixture now — while the entity set is small — is far
cheaper than discovering rail-only assumptions after four milestones of code has been written against them.

## Preconditions

- `domain/geometry/` is complete and tested: `Alignment`, `HorizontalAlignment`, `VerticalAlignment`,
  `CantProfile`, `RotationPivot`, plus `line`, `arc`, `clothoid` segment types.
- `domain/lrs.py` provides `frames`, `to_xyz`, `project_xy`; `domain/sampling.py` provides `bake_frame_table`.
- `networkx>=3.4` is already a declared dependency and currently unused.
- **Naming collision:** `domain/geometry/horizontal.py` already defines a dataclass named `Junction` — an
  alignment-internal *discontinuity report* (gap, heading jump, curvature jump between consecutive segments),
  produced by `HorizontalAlignment.junctions()`. It has nothing to do with ADR 0006's topological junction,
  it is referenced nowhere else in the codebase, and it occupies the name the domain model needs.

Read before starting: `docs/adr/0006-multimodal-lrs-model.md`, `docs/adr/0005-document-format.md`,
`docs/data-contracts/coordinate-conventions.md`, `domain/geometry/alignment.py`, `domain/lrs.py`,
`domain/model/modes.py`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/geometry/horizontal.py` | rename `Junction` → `SegmentDiscontinuity`, `junctions()` → `discontinuities()` |
| `backend/src/coypu_builder/domain/model/ids.py` | new — entity id type and generation |
| `backend/src/coypu_builder/domain/model/anchor.py` | new — `LrsAnchor` |
| `backend/src/coypu_builder/domain/model/network.py` | new — `Network`, `Node`, `Link`, `Junction`, `JunctionKind` |
| `backend/src/coypu_builder/domain/model/corridor.py` | new — `Corridor`, `CrossSectionInterval` (declaration only) |
| `backend/src/coypu_builder/domain/model/asset.py` | new — `Asset` |
| `backend/src/coypu_builder/domain/model/organisation.py` | new — `Layer`, `WbsNode` |
| `backend/src/coypu_builder/domain/model/__init__.py` | re-export the public names |
| `backend/src/coypu_builder/domain/topology/graph.py` | new — networkx-backed graph over a `Network` |
| `backend/src/coypu_builder/domain/topology/__init__.py` | re-export |
| `backend/tests/fixtures/synthetic/__init__.py` | new |
| `backend/tests/fixtures/synthetic/tram_loop.py` | new — code-generated tram network, no data files |
| `backend/tests/test_domain_model.py` | new |
| `backend/tests/test_topology.py` | new |
| `backend/tests/test_multimodal.py` | new — the mode-agnosticism assertions |
| `backend/tests/conftest.py` | add fixtures exposing the tram network |
| `docs/data-contracts/entity-model.md` | new — the entity reference, written from what you built |

## Contract

### Identity

```python
EntityId = str   # uuid4().hex — 32 lowercase hex characters

def new_id() -> EntityId: ...
```

ADR 0005 maps these to IFC `GlobalId` via `ifcopenshell.guid.compress` at export time. Do **not** import
ifcopenshell here; `domain/` stays pure and that dependency is optional and lazy.

### Placement

```python
@dataclass(frozen=True, slots=True)
class LrsAnchor:
    alignment_id: EntityId
    s: float                    # absolute station, metres
    y: float = 0.0              # left-positive lateral offset in the track plane
    z: float = 0.0              # offset along the track-plane normal
    heading_offset: float = 0.0 # radians, ccw, relative to the alignment tangent
```

This is the universal placement for every positioned entity in the system. It carries no mode-specific field
and never will.

### Network and topology

```python
class JunctionKind(StrEnum):
    TURNOUT = "turnout"
    CROSSING = "crossing"
    SLIP = "slip"
    INTERSECTION = "intersection"     # road / tram street intersection
    BUFFER_STOP = "buffer_stop"
    CONNECTION = "connection"         # plain end-to-end continuity

@dataclass(frozen=True, slots=True)
class Node:
    id: EntityId
    position: tuple[float, float, float]     # (E, N, H) float64, project CRS
    junction: Junction | None = None         # None = a plain topological node
    name: str = ""

@dataclass(frozen=True, slots=True)
class Junction:
    kind: JunctionKind
    properties: Mapping[str, object] = ...    # kind-specific, e.g. turnout hand/radius; generated
                                              # geometry is Phase 2 and must not be modelled here

@dataclass(frozen=True, slots=True)
class Link:
    id: EntityId
    alignment_id: EntityId
    start_node: EntityId
    end_node: EntityId
    s_start: float          # station range of the alignment this link occupies
    s_end: float
    name: str = ""

@dataclass(frozen=True, slots=True)
class Network:
    id: EntityId
    mode: Mode
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]
    name: str = ""
```

A link's `s_start`/`s_end` may be descending — that expresses traversal against the alignment's station
direction and must be handled, not rejected.

`domain/topology/graph.py` builds an undirected `networkx.MultiGraph` (a node pair may be joined by more than
one link — a passing loop is the obvious case) with nodes keyed by `EntityId` and edges carrying the `Link`.
Provide at minimum:

```python
def build_graph(network: Network) -> nx.MultiGraph: ...
def connected_components(network: Network) -> list[set[EntityId]]: ...
def shortest_path_links(network: Network, start: EntityId, end: EntityId) -> list[Link]: ...
def degree(network: Network, node_id: EntityId) -> int: ...
def validate(network: Network) -> list[str]: ...   # warnings: dangling link endpoints, duplicate ids,
                                                   # junction kind inconsistent with node degree, zero-length
                                                   # links, links whose station range leaves the alignment
```

`validate` returns warnings, never raises. A degree-3 node with no `Junction` and a degree-2 node declared
`TURNOUT` are both warnings.

### Corridor, Asset, organisation

```python
@dataclass(frozen=True, slots=True)
class CrossSectionInterval:
    s_start: float
    s_end: float
    section_id: str          # catalogue key; the section library itself is Phase 2
    parameters: Mapping[str, float] = ...

@dataclass(frozen=True, slots=True)
class Corridor:
    id: EntityId
    alignment_id: EntityId
    intervals: tuple[CrossSectionInterval, ...] = ()
    name: str = ""

@dataclass(frozen=True, slots=True)
class Asset:
    id: EntityId
    catalogue_id: str
    anchor: LrsAnchor
    layer_id: EntityId | None = None
    properties: Mapping[str, object] = ...
    name: str = ""

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

`Corridor` and `CrossSectionInterval` are **declared, not implemented**: they must exist as types so that the
document schema and the protocol can reference them, but no section geometry, no sweeping and no library
lands in this task. Roadmap decision 3 keeps `domain/sections/` empty until Phase 2.

### Synthetic tram fixture

`tests/fixtures/synthetic/tram_loop.py` builds, purely in code with no data files:

- Two tram alignments in a projected metric CRS of your choosing (EPSG:5514 keeps it consistent with the
  Kralupy fixture): a street section and a terminal loop, each a small line/arc/line chain built from the
  existing segment primitives, with a flat or gently graded `VerticalAlignment`.
- `Mode.LIGHT_RAIL_TRAM`, **1000 mm gauge** and a superelevation base matching it — this is the point of the
  fixture. Cant handling, the low-rail pivot and the frame maths must work on a non-1435 mm base.
- A `Network` with at least four nodes and three links, containing one `JunctionKind.TURNOUT` node of degree
  three (the loop's entry) and one `BUFFER_STOP`.
- A builder function returning `(Network, dict[EntityId, Alignment])` so tests and later tasks can use it.

Geometry realism is not the goal; topological and modal variety is.

## Invariants

- **`domain/` is pure.** numpy / scipy / pyproj / networkx only. No I/O, no protocol types, no msgspec, no
  file reading — the tram fixture is built in code precisely so that this holds.
- **ADR 0006:** no rail-only assumption in `domain/lrs.py` or `domain/model`. `Mode` tags semantics only.
  If you find one while building the tram fixture, that is the fixture doing its job — fix it and say so.
- Frozen dataclasses for domain values (`CLAUDE.md` conventions), `slots=True` where it costs nothing.
- Do not add comments that restate the code.

## Acceptance criteria

1. The `Junction` rename is complete; no reference to the old name survives; existing tests still pass.
2. `test_multimodal.py` asserts, for the tram network, that: `frames()` produces correct roll for a 1000 mm
   superelevation base; `to_xyz` and `project_xy` round-trip on a tram alignment; `bake_frame_table` works;
   and no code path branches on `Mode.HEAVY_RAIL` to produce a different result where it should not.
3. `validate()` returns the expected warnings for a deliberately malformed network (dangling endpoint,
   duplicate id, `TURNOUT` on a degree-2 node) and an empty list for the tram fixture.
4. `shortest_path_links` finds a route across the tram junction; `connected_components` reports one component
   for the tram fixture and two when a link is removed.
5. A link with `s_start > s_end` is handled without error by everything that consumes it.
6. `docs/data-contracts/entity-model.md` documents every entity, its fields and the LRS anchor convention,
   and is accurate against the code you wrote.
7. `uv run ruff check .` clean and `uv run pytest` green.

## Out of scope

- Any persistence — the `.coypub` schema is T-145.
- Any protocol exposure of these entities — that is T-114 and later.
- Turnout or crossing **geometry** generation. `Junction` carries semantics and properties only.
- Cross-section libraries, sweeping, `domain/sections/`.
- `KinematicsRun` and `Trainset` — they are T-111 and T-113 respectively, deliberately split out so these
  three tasks can run in parallel. Do not define them here, not even as stubs.

## Verification

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

## Report back

State: any rail-only assumption the tram fixture exposed in existing code and how you resolved it; the final
entity field lists where they deviate from this contract and why; what `validate()` checks; and whether the
`Junction` rename touched anything unexpected.
