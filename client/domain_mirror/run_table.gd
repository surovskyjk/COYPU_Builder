class_name RunTable
extends RefCounted
## Client-side mirror of one `run.get` response (ADR 0007): a kinematics run resampled by
## `domain/kinematics.py: bake_run_table` onto a uniform time grid, so there is no time blob and no
## search — `t = i * dt` reconstructs the row index directly. Force blobs
## (`f_traction`/`f_braking`/`f_resistance`) are omitted from the wire entirely, never zero-filled, when
## the source run carries none; [method has_forces] reports that, and [method traction_at] returns `0.0`
## rather than faking a value when it does.
##
## Pinned to `shared/golden/run_table.json` by `client/tests/unit/test_run_table.gd`.

var _run_id := ""
var _dt := 0.0
var _duration := 0.0
var _row_count := 0
var _direction := 1
var _stops: Array[Dictionary] = []

var _station: PackedFloat32Array
var _speed: PackedFloat32Array
var _accel: PackedFloat32Array
var _traction: PackedFloat32Array
var _braking: PackedFloat32Array
var _resistance: PackedFloat32Array
var _has_forces := false


static func from_envelope(envelope: IpcEnvelope) -> RunTable:
	var table := RunTable.new()
	table._run_id = str(envelope.result.get("run_id", ""))
	table._dt = float(envelope.result.get("dt", 0.0))
	table._row_count = int(envelope.result.get("row_count", 0))
	table._duration = float(envelope.result.get("duration_s", 0.0))
	table._direction = int(envelope.result.get("direction", 1))

	for entry: Variant in envelope.result.get("stops", []):
		if entry is Dictionary:
			table._stops.append(entry)

	table._station = envelope.blobs.get("station", PackedFloat32Array())
	table._speed = envelope.blobs.get("speed", PackedFloat32Array())
	table._accel = envelope.blobs.get("accel", PackedFloat32Array())
	table._traction = envelope.blobs.get("f_traction", PackedFloat32Array())
	table._braking = envelope.blobs.get("f_braking", PackedFloat32Array())
	table._resistance = envelope.blobs.get("f_resistance", PackedFloat32Array())
	table._has_forces = envelope.blobs.has("f_traction")
	return table


func run_id() -> String:
	return _run_id


func dt() -> float:
	return _dt


func duration() -> float:
	return _duration


func row_count() -> int:
	return _row_count


func direction() -> int:              # +1 forward, -1 reverse
	return _direction


func stops() -> Array[Dictionary]:
	return _stops


func has_forces() -> bool:
	return _has_forces


## O(1): i = t / dt, then lerp with the next row. Clamped at both ends.
func station_at(t: float) -> float:
	return _sample(_station, t)


func speed_at(t: float) -> float:
	return _sample(_speed, t)


func accel_at(t: float) -> float:
	return _sample(_accel, t)


## 0.0 when the run carries no force data; check [method has_forces] first.
func traction_at(t: float) -> float:
	if not _has_forces:
		return 0.0
	return _sample(_traction, t)


## During a dwell the source column is flat and speed is zero — lerping two equal-ish neighbouring rows
## is itself flat, so no dwell/boundary special case is needed here.
func _sample(column: PackedFloat32Array, t: float) -> float:
	var n := column.size()
	if n == 0:
		return 0.0
	if n == 1 or _dt <= 0.0:
		return column[0]

	var clamped := clampf(t, 0.0, _duration)
	var f := clamped / _dt
	var i := int(floor(f))
	if i >= n - 1:
		return column[n - 1]
	if i < 0:
		i = 0
	var frac := f - float(i)
	return lerpf(column[i], column[i + 1], frac)
