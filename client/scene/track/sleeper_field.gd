class_name SleeperField
extends MultiMeshInstance3D
## One chunk's sleepers, placed directly from [AlignmentTable] (ADR 0007: per-instance placement is
## evaluation, and evaluation belongs in the client, never the backend). Each instance's transform is
## [method AlignmentTable.sample]'s `position`/`rotation` at that sleeper's station, offset downward along
## the frame's own up axis so the sleeper top meets the rail foot -- sleepers therefore inherit cant roll
## and gradient pitch exactly as the rails do, by construction, with no separate roll/pitch math here.
##
## **ADR 0004**: like a [TrackChunk], a sleeper field is tile-local -- [method populate] subtracts
## `tile_origin` from every instance position and puts the origin on this node's own `position`, or the
## float32 instance transforms would shimmer at corridor scale exactly as un-tiled vertices would.

const DEFAULT_SPACING_M := 0.6  # sleeper pitch
const SLEEPER_SIZE := Vector3(2.6, 0.16, 0.26)  # length across track, height, width along track

## Depth of the rail foot below the track plane (`AlignmentTable.sample().position`, which sits at
## rail-head-top level -- see docs/data-contracts/track-mesh.md), matching
## `io/mesh/profiles.py: DEFAULT_RAIL_HEIGHT_M`. Like [constant SLEEPER_SIZE], this is a documented
## placeholder: the frame table carries no rail-profile data for this to read back from the alignment
## itself (ADR 0006 -- nothing here assumes a specific gauge or rail section).
const RAIL_FOOT_DEPTH_M := 0.172

static var _shared_mesh: BoxMesh


func _init() -> void:
	multimesh = MultiMesh.new()
	multimesh.transform_format = MultiMesh.TRANSFORM_3D
	multimesh.mesh = _sleeper_mesh()
	material_override = TrackMaterials.sleeper_material()


## Places one instance per sleeper station across `[from_station, to_station]` (inclusive of
## `from_station`, up to the last multiple of `spacing_m` that does not exceed `to_station`).
func populate(
	table: AlignmentTable,
	from_station: float,
	to_station: float,
	spacing_m: float = DEFAULT_SPACING_M,
	tile_origin: Vector3 = Vector3.ZERO
) -> void:
	position = tile_origin

	var span := to_station - from_station
	var count := 0
	if spacing_m > 0.0 and span >= 0.0:
		count = int(floor(span / spacing_m)) + 1

	multimesh.instance_count = count
	for i in count:
		var station := from_station + float(i) * spacing_m
		multimesh.set_instance_transform(i, transform_for_station(table, station, tile_origin))


## One sleeper's tile-local transform at `station`: [method AlignmentTable.sample]'s position/rotation,
## offset down the frame's own up axis so the sleeper top meets the rail foot -- cant roll and gradient
## pitch come along automatically since the basis is taken straight from the sampled frame, never
## re-derived. Pure and side-effect-free (unlike [method populate], it never touches [member multimesh]),
## which is what makes it directly unit-testable: the [MultiMesh]/RenderingServer's own instance-transform
## storage does not round-trip under Godot's headless dummy renderer, so tests assert against this
## function's return value instead of reading transforms back out of a built field.
static func transform_for_station(table: AlignmentTable, station: float, tile_origin: Vector3) -> Transform3D:
	var sample := table.sample(station)
	var basis := Basis(sample.rotation)
	var drop := SLEEPER_SIZE.y * 0.5 + RAIL_FOOT_DEPTH_M
	var local_position := (sample.position - tile_origin) - basis.y * drop
	return Transform3D(basis, local_position)


static func _sleeper_mesh() -> BoxMesh:
	if _shared_mesh == null:
		_shared_mesh = BoxMesh.new()
		_shared_mesh.size = SLEEPER_SIZE
	return _shared_mesh
