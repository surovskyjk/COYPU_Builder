class_name OrbitCamera
extends CameraRig
## Default camera (T-124): pivots about a focus point, mouse-drag to rotate, wheel to zoom, middle-drag to
## pan. Distance-scaled pan speed and multiplicative zoom keep it equally usable at 5 m and at 5 km, and
## the elevation is clamped a few degrees short of vertical so [method Node3D.look_at] never receives a
## view direction parallel to its up hint (the gimbal flip AC2 calls out) -- clamping is simpler and
## cheaper than special-casing the exact pole.
##
## Pitch convention: `_pitch` (and [method pitch]) is the camera's *elevation above the horizontal plane
## through the focus*, in radians. Positive is above, so the camera's height over the focus is
## `distance * sin(pitch)`. It is clamped to `[MIN_ELEVATION, _PITCH_LIMIT]`: the camera never drops below
## the focus plane (once M3 adds terrain, a view from below the ground shows nothing). Dragging the mouse
## up (negative `relative.y`) raises the elevation, so the view becomes more top-down.
##
## "Follows the consist with damping": only the *chase* -- the anchor point easing toward the lead car's
## position -- is damped ([constant FOLLOW_TAU_SEC]); the user's own rotate/zoom/pan input is applied
## immediately, with no lag of its own, so the camera stays responsive to the mouse the way a sandbox
## game's does, rather than feeling like a CAD viewport waiting to catch up. A manual pan persists as an
## offset from the (still-following) anchor rather than fighting it every frame.

## Chosen so a mid-speed passing train (Kralupy's fixture runs at highway-ish speeds) drifts smoothly into
## frame over roughly half a second rather than snapping to its new position or trailing so far behind
## that a fast train visibly outruns the pivot.
const FOLLOW_TAU_SEC := 0.6

const MIN_DISTANCE_M := 1.0
const MAX_DISTANCE_M := 20000.0
const DEFAULT_DISTANCE_M := 60.0

const ROTATE_SPEED := 0.005
const ZOOM_STEP_FACTOR := 0.9
const PAN_SPEED := 0.0015

## Lowest elevation (about 1 degree) above the focus plane; the camera never goes below it.
const MIN_ELEVATION := 0.02

const DEFAULT_PITCH := 0.35

## A few degrees short of PI/2: beyond that the view direction and the `Vector3.UP` hint `look_at` uses
## become parallel and the resulting basis degenerates (AC2's "no gimbal flip at the pole").
const _PITCH_LIMIT := 1.45

var _trainset: TrainsetNode
var _table: AlignmentTable

var _anchor := Vector3.ZERO
var _damped_anchor := Vector3.ZERO
var _pan_offset := Vector3.ZERO

var _yaw := -0.6
var _pitch := DEFAULT_PITCH
var _distance := DEFAULT_DISTANCE_M

var _rotating := false
var _panning := false


func distance() -> float:
	return _distance


func pitch() -> float:
	return _pitch


func bind_subject(trainset: TrainsetNode, _controller: PlaybackController) -> void:
	_trainset = trainset


## The corridor mesh itself is irrelevant to the orbit camera; only the table's station range matters, to
## give [method focus_station] something to place the pivot against as soon as one exists.
func bind_corridor(_corridor: TrackCorridor, table: AlignmentTable) -> void:
	_table = table
	if table != null:
		focus_station(table.station_start())


func focus_station(s: float) -> void:
	if _table == null:
		return
	_anchor = _table.position_at(s)
	_pan_offset = Vector3.ZERO
	_damped_anchor = _anchor


func activated() -> void:
	_damped_anchor = _target_anchor()


func update(delta: float) -> void:
	_damped_anchor = CameraRig.damp_vector(_damped_anchor, _target_anchor(), delta, FOLLOW_TAU_SEC)
	var focus := _damped_anchor + _pan_offset

	var direction := Vector3(cos(_pitch) * sin(_yaw), sin(_pitch), cos(_pitch) * cos(_yaw))
	_camera.global_position = focus + direction * _distance
	_camera.look_at(focus, Vector3.UP)


## Actions, not scancodes (`project.godot`'s `[input]` map: `camera_orbit_rotate`/`camera_orbit_pan`/
## `camera_zoom_in`/`camera_zoom_out`) -- so M4 can rebind mouse-drag rotate/pan and wheel zoom without
## touching this file.
func handle_input(event: InputEvent) -> void:
	if event.is_action_pressed("camera_orbit_rotate"):
		_rotating = true
	elif event.is_action_released("camera_orbit_rotate"):
		_rotating = false
	elif event.is_action_pressed("camera_orbit_pan"):
		_panning = true
	elif event.is_action_released("camera_orbit_pan"):
		_panning = false
	elif event.is_action_pressed("camera_zoom_in"):
		_zoom(ZOOM_STEP_FACTOR)
	elif event.is_action_pressed("camera_zoom_out"):
		_zoom(1.0 / ZOOM_STEP_FACTOR)
	elif event is InputEventMouseMotion and (_rotating or _panning):
		var motion := (event as InputEventMouseMotion).relative
		if _rotating:
			_yaw -= motion.x * ROTATE_SPEED
			_pitch = clampf(_pitch - motion.y * ROTATE_SPEED, MIN_ELEVATION, _PITCH_LIMIT)
		if _panning:
			var right := _camera.global_transform.basis.x
			var up := _camera.global_transform.basis.y
			_pan_offset += (-right * motion.x + up * motion.y) * PAN_SPEED * _distance


## Distance-scaled zoom (task requirement): a fixed *fraction* per wheel notch, not a fixed metre step, so
## a notch is equally meaningful whether the camera is 5 m or 5 km out.
func _zoom(factor: float) -> void:
	_distance = clampf(_distance * factor, MIN_DISTANCE_M, MAX_DISTANCE_M)


func _target_anchor() -> Vector3:
	if _trainset != null and _trainset.car_count() > 0:
		return _trainset.car(0).body().global_position
	return _anchor
