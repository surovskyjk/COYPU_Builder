class_name AlignmentTable
extends RefCounted
## Client-side mirror of one `alignment.frame_table` response (ADR 0007): the backend bakes a dense,
## non-uniformly spaced station grid (`domain/sampling.py: bake_stations` — a uniform grid merged with
## every geometric key station and refined by chord error near curves), and this table only interpolates
## between rows the backend already produced. It never constructs geometry, evaluates a clothoid or
## recomputes cant roll itself (ADR 0001).
##
## `position`/`roll`/`pitch`/`cant_mm`/`curvature`/`gradient`/`elevation` interpolate linearly between
## the bracketing rows; `rotation` uses [method Quaternion.slerp], never a linear/Euler blend;
## `segment_index` takes the lower row's value — a category, not a quantity. A query outside
## `[station_start, station_end]` clamps to the first/last row rather than extrapolating.
##
## Positions and rotations arrive already base-point-relative and in Godot axes (ADR 0004) — never route
## them through [Origin]; that would double-apply the mapping.
##
## Pinned to `shared/golden/frame_eval.json` by `client/tests/unit/test_alignment_table.gd`.

var _alignment_id := ""
var _row_count := 0
var _station_start := 0.0
var _station_end := 0.0

var _stations: PackedFloat32Array
var _positions: PackedVector3Array
var _rotations: PackedFloat32Array        # flat (x, y, z, w) per row — cols=4 decodes flat, not Vector4
var _roll: PackedFloat32Array
var _pitch: PackedFloat32Array
var _cant_mm: PackedFloat32Array
var _curvature: PackedFloat32Array
var _gradient: PackedFloat32Array
var _elevation: PackedFloat32Array
var _segment_index: PackedInt32Array

## Forward-walk hint for [method index_of]: callers overwhelmingly query increasing stations frame to
## frame (camera follow, playback scrubbing forward), so the common case is an O(1) bounds check instead
## of a fresh binary search.
var _last_index := 0


## Built from an alignment.frame_table response: the result Dictionary plus the decoded blobs.
static func from_envelope(envelope: IpcEnvelope) -> AlignmentTable:
	var table := AlignmentTable.new()
	table._alignment_id = str(envelope.result.get("alignment_id", ""))
	table._row_count = int(envelope.result.get("row_count", 0))
	table._station_start = float(envelope.result.get("station_start", 0.0))
	table._station_end = float(envelope.result.get("station_end", 0.0))

	table._stations = envelope.blobs.get("station", PackedFloat32Array())
	table._positions = envelope.blobs.get("position", PackedVector3Array())
	table._rotations = envelope.blobs.get("rotation", PackedFloat32Array())
	table._roll = envelope.blobs.get("roll", PackedFloat32Array())
	table._pitch = envelope.blobs.get("pitch", PackedFloat32Array())
	table._cant_mm = envelope.blobs.get("cant_mm", PackedFloat32Array())
	table._curvature = envelope.blobs.get("curvature", PackedFloat32Array())
	table._gradient = envelope.blobs.get("gradient", PackedFloat32Array())
	table._elevation = envelope.blobs.get("elevation", PackedFloat32Array())
	table._segment_index = envelope.blobs.get("segment_index", PackedInt32Array())
	return table


func alignment_id() -> String:
	return _alignment_id


func row_count() -> int:
	return _row_count


func station_start() -> float:
	return _station_start


func station_end() -> float:
	return _station_end


## Row index of the last station <= s. Binary search with the forward-walk cache above; -1 before the
## first row; the last row index at or beyond the final station (it never reports past-the-end).
func index_of(station: float) -> int:
	var n := _stations.size()
	if n == 0:
		return -1
	if station < _stations[0]:
		return -1
	if station >= _stations[n - 1]:
		_last_index = n - 1
		return n - 1

	if (
		_last_index >= 0
		and _last_index < n - 1
		and _stations[_last_index] <= station
		and station < _stations[_last_index + 1]
	):
		return _last_index

	var lo := 0
	var hi := n - 1
	while lo < hi:
		var mid := (lo + hi + 1) / 2
		if _stations[mid] <= station:
			lo = mid
		else:
			hi = mid - 1
	_last_index = lo
	return lo


## Interpolated sample at an absolute station, clamped to the table's range.
func sample(station: float) -> FrameSample:
	var fs := FrameSample.new()
	var n := _stations.size()
	if n == 0:
		return fs

	var clamped := clampf(station, _stations[0], _stations[n - 1])
	var i := index_of(clamped)
	if i < 0:
		i = 0
	fs.station = clamped

	if i >= n - 1:
		_fill_exact(fs, n - 1)
		return fs

	var s0 := _stations[i]
	var s1 := _stations[i + 1]
	var t := 0.0 if s1 <= s0 else (clamped - s0) / (s1 - s0)
	_fill_interpolated(fs, i, i + 1, t)
	return fs


## Position only — the hot path for camera follow; avoids building a FrameSample.
func position_at(station: float) -> Vector3:
	var n := _positions.size()
	if n == 0:
		return Vector3.ZERO

	var clamped := clampf(station, _stations[0], _stations[n - 1])
	var i := index_of(clamped)
	if i < 0:
		i = 0
	if i >= n - 1:
		return _positions[n - 1]

	var s0 := _stations[i]
	var s1 := _stations[i + 1]
	var t := 0.0 if s1 <= s0 else (clamped - s0) / (s1 - s0)
	return _positions[i].lerp(_positions[i + 1], t)


## Raw column access for bulk consumers (track meshing, diagnostics ramps). Do not mutate.
func stations() -> PackedFloat32Array:
	return _stations


## Raw column access for bulk consumers (track meshing, diagnostics ramps). Do not mutate.
func positions() -> PackedVector3Array:
	return _positions


func _quat_at(i: int) -> Quaternion:
	var base := i * 4
	if base + 3 >= _rotations.size():
		return Quaternion.IDENTITY
	return Quaternion(_rotations[base], _rotations[base + 1], _rotations[base + 2], _rotations[base + 3])


func _f32(column: PackedFloat32Array, i: int) -> float:
	return column[i] if i < column.size() else 0.0


func _i32(column: PackedInt32Array, i: int) -> int:
	return column[i] if i < column.size() else 0


func _fill_exact(fs: FrameSample, i: int) -> void:
	fs.station = _stations[i]
	fs.position = _positions[i]
	fs.rotation = _quat_at(i)
	fs.roll = _f32(_roll, i)
	fs.pitch = _f32(_pitch, i)
	fs.cant_mm = _f32(_cant_mm, i)
	fs.curvature = _f32(_curvature, i)
	fs.gradient = _f32(_gradient, i)
	fs.elevation = _f32(_elevation, i)
	fs.segment_index = _i32(_segment_index, i)


func _fill_interpolated(fs: FrameSample, i: int, j: int, t: float) -> void:
	fs.position = _positions[i].lerp(_positions[j], t)
	fs.rotation = _quat_at(i).slerp(_quat_at(j), t)
	fs.roll = lerpf(_f32(_roll, i), _f32(_roll, j), t)
	fs.pitch = lerpf(_f32(_pitch, i), _f32(_pitch, j), t)
	fs.cant_mm = lerpf(_f32(_cant_mm, i), _f32(_cant_mm, j), t)
	fs.curvature = lerpf(_f32(_curvature, i), _f32(_curvature, j), t)
	fs.gradient = lerpf(_f32(_gradient, i), _f32(_gradient, j), t)
	fs.elevation = lerpf(_f32(_elevation, i), _f32(_elevation, j), t)
	# A category, not a quantity — takes the lower row's value rather than interpolating.
	fs.segment_index = _i32(_segment_index, i)
