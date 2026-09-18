extends Node
## Autoload `Origin`: the Project Base Point and the domain (E, N, H) <-> Godot axis mapping (ADR 0004).
##
## The backend already maps every position it sends into Godot axes relative to the base point
## (`server/handlers.py` calls `points_to_godot` / `basis_to_godot`) — this autoload does [b]not[/b]
## transform incoming geometry; doing so would double-apply the mapping (ADR 0001). Its jobs are to hold
## the base point for display, map a Godot position [i]back[/i] to (E, N, H) for inspector/status
## readouts and picking, and be the one place the client knows the axis convention, pinned to the
## backend by `shared/golden/origin_mapping.json`.
##
## Precision: every computation below stays in GDScript's 64-bit [float] scalars until the final
## [Vector3]/[Basis] is constructed, mirroring `domain/crs.py`'s float64 math exactly. [method
## base_point] is for display only — round-tripping through it loses precision to the engine's
## single-precision [Vector3]; [method to_godot] and [method from_godot] never route through it.

signal base_point_changed(easting: float, northing: float, height: float)

var _has_base_point := false
var _base_easting := 0.0
var _base_northing := 0.0
var _base_height := 0.0


func set_base_point(easting: float, northing: float, height: float) -> void:
	_base_easting = easting
	_base_northing = northing
	_base_height = height
	_has_base_point = true
	base_point_changed.emit(easting, northing, height)


func has_base_point() -> bool:
	return _has_base_point


## (E0, N0, H0) for display. See the precision note above — use [method to_godot]/[method from_godot]
## for anything that must stay exact against the golden vectors.
func base_point() -> Vector3:
	return Vector3(_base_easting, _base_northing, _base_height)


## (E, N, H) -> Godot. Present for readouts and tests, NOT for transforming backend geometry.
func to_godot(easting: float, northing: float, height: float) -> Vector3:
	var x := easting - _base_easting
	var y := height - _base_height
	var z := -(northing - _base_northing)
	return Vector3(x, y, z)


## Godot -> (E, N, H). Returns a Vector3 carrying (E, N, H) in that order.
func from_godot(local: Vector3) -> Vector3:
	var easting := float(local.x) + _base_easting
	var northing := _base_northing - float(local.z)
	var height := float(local.y) + _base_height
	return Vector3(easting, northing, height)


## Domain frame columns -> Godot Basis, columns (right, up, back); -Z is the tangent.
func basis_from_frame(tangent: Vector3, left: Vector3, up: Vector3) -> Basis:
	var right := -_vector_to_godot(left)
	var up_col := _vector_to_godot(up)
	var back := -_vector_to_godot(tangent)
	return Basis(right, up_col, back)


## Direction mapping (no base-point translation): (E, N, H) -> (E, H, -N).
static func _vector_to_godot(v: Vector3) -> Vector3:
	return Vector3(v.x, v.z, -v.y)
